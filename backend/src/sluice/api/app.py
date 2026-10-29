"""FastAPI application factory: contract error bodies, CORS on every response, health check.

Every 4xx/5xx body is ``{"detail": str, "code"?: str}`` (``docs/api/contract.md``). Middleware
order, outermost first::

    ServerErrorMiddleware (Starlette) -> CORSMiddleware -> InternalErrorMiddleware -> routes

An ``exception_handler(Exception)`` would run in Starlette's outermost ``ServerErrorMiddleware``,
outside CORS, so the browser would see a 500 without ``Access-Control-Allow-Origin`` and report a
network error. ``InternalErrorMiddleware`` sits *inside* CORS instead, so the generic 500 carries
the CORS headers like every other response.
"""

import asyncio
import itertools
import logging
import traceback
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Final

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from sluice import tracing
from sluice.api.blobs import BlobStore, S3BlobStore
from sluice.api.hub import EventHub
from sluice.api.routes_events import router as events_router
from sluice.api.schemas import ErrorBody
from sluice.api.settings import ApiSettings
from sluice.api.store import RunStore
from sluice.api.worker import ExecuteFn, Worker
from sluice.runs.execute import execute_run
from sluice.settings import get_settings

logger = logging.getLogger(__name__)

INTERNAL_DETAIL: Final = "Internal error."
MAX_DETAIL_CHARS: Final = 500
"""Longest validation ``detail``; FastAPI messages are short, this only bounds pathological ones."""
MAX_LOGGED_CHAIN: Final = 8
"""Exceptions of a ``__cause__``/``__context__`` chain written to the log."""
MAX_LOGGED_FRAMES: Final = 64
"""Frames per exception written to the log."""

_CODES_BY_STATUS: Final[dict[int, str]] = {
    400: "bad_request",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    413: "too_large",
    415: "unsupported_media_type",
    422: "invalid_request",
}
_DETAILS_BY_STATUS: Final[dict[int, str]] = {
    404: "Not found.",
    405: "Method not allowed.",
}


class ApiError(Exception):
    """An expected failure a route reports to the client as ``{detail, code}``.

    Args:
        status: HTTP status, 400-599.
        detail: User-facing message. Never put secrets, paths or exception text here.
        code: Optional machine-readable code (``too_large``, ``not_ready``, ...).
    """

    def __init__(self, status: int, detail: str, code: str | None = None) -> None:
        if not 400 <= status <= 599:
            raise ValueError(f"ApiError status must be 4xx or 5xx, got {status}")
        super().__init__(detail)
        self.status = status
        self.detail = detail
        self.code = code


def error_response(
    status: int, detail: str, code: str | None = None, headers: dict[str, str] | None = None
) -> JSONResponse:
    """The contract error body; ``code`` is omitted when ``None``."""
    body = ErrorBody(detail=detail, code=code).model_dump(exclude_none=True)
    return JSONResponse(body, status_code=status, headers=headers)


def _validation_detail(exc: RequestValidationError) -> str:
    """``"Invalid request: query.limit: Input should be ..."``. Never echoes the submitted input."""
    errors = list(exc.errors())
    if not errors:
        return "Invalid request."
    first = errors[0]
    where = ".".join(str(part) for part in first.get("loc", ()))
    message = str(first.get("msg", "invalid value"))
    detail = f"Invalid request: {where}: {message}" if where else f"Invalid request: {message}"
    if len(errors) > 1:
        detail += f" (and {len(errors) - 1} more)"
    return detail[:MAX_DETAIL_CHARS]


async def _http_exception(_: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, StarletteHTTPException)
    status = exc.status_code
    detail = exc.detail if isinstance(exc.detail, str) and exc.detail else ""
    # Starlette's own "Not Found" / "Method Not Allowed" get sentence-style copy for the UI.
    if not detail or detail in ("Not Found", "Method Not Allowed"):
        detail = _DETAILS_BY_STATUS.get(status, "Request failed.")
    return error_response(status, detail, _CODES_BY_STATUS.get(status), exc.headers)


async def _request_validation(_: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)
    return error_response(422, _validation_detail(exc), "invalid_request")


async def _api_error(_: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, ApiError)
    return error_response(exc.status, exc.detail, exc.code)


def _type_name(exc: BaseException) -> str:
    """``module.QualName`` of the exception class (just ``RuntimeError`` for builtins)."""
    cls = type(exc)
    if cls.__module__ == "builtins":
        return cls.__qualname__
    return f"{cls.__module__}.{cls.__qualname__}"


def redacted_traceback(exc: BaseException) -> str:
    """The exception chain as type names and ``file:line:function`` frames, nothing else.

    Exception messages (``str(exc)``, of the exception and of every ``__cause__`` /
    ``__context__``) and source lines are never included: either can hold a secret such as a DSN
    or an API key. Walks the chain outermost first, at most :data:`MAX_LOGGED_CHAIN` exceptions
    of at most :data:`MAX_LOGGED_FRAMES` frames each, and stops on a cycle.
    """
    sections: list[str] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen and len(sections) < MAX_LOGGED_CHAIN:
        seen.add(id(current))
        frames = [
            f"  at {frame.f_code.co_filename}:{lineno}:{frame.f_code.co_name}"
            for frame, lineno in itertools.islice(
                traceback.walk_tb(current.__traceback__), MAX_LOGGED_FRAMES
            )
        ]
        sections.append("\n".join([_type_name(current), *frames]))
        if current.__cause__ is not None:
            current, link = current.__cause__, "caused by"
        elif current.__context__ is not None and not current.__suppress_context__:
            current, link = current.__context__, "during handling of"
        else:
            break
        sections[-1] += f"\n{link}:"
    return "\n".join(sections)


class InternalErrorMiddleware:
    """Turns an unhandled exception into the generic contract 500, inside CORS.

    The exception is logged server-side as type names and frame locations only
    (:func:`redacted_traceback`), never its message; the body never contains either. If the
    response has already started (e.g. mid-way through an SSE stream) no new response can be
    sent, so the exception propagates and the connection is closed.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = False

        async def tracking_send(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, receive, tracking_send)
        except Exception as exc:
            logger.error(
                "unhandled %s in %s %s\n%s",
                _type_name(exc),
                scope.get("method"),
                scope.get("path"),
                redacted_traceback(exc),
            )
            if started:
                raise
            await error_response(500, INTERNAL_DETAIL, "internal")(scope, receive, send)


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Create the event hub; with the worker enabled, recover interrupted runs, then start it.

    A store or blob store not injected is built from the service settings (``make api``):
    :class:`RunStore` on the stack Postgres (schema ensured) and :class:`S3BlobStore` on the
    landing bucket. Both are built in a thread: each does blocking I/O.
    """
    state = app.state
    state.hub = EventHub()
    state.worker = None
    if state.worker_enabled:
        if state.store is None:
            store = RunStore(get_settings().postgres_dsn)
            await asyncio.to_thread(store.ensure_schema)
            state.store = store
        if state.blobs is None:
            state.blobs = await asyncio.to_thread(
                S3BlobStore.from_settings, get_settings(), state.settings.landing_bucket
            )
        worker = Worker(
            state.store,
            state.blobs,
            state.hub,
            concurrency=state.settings.concurrency,
            execute=state.execute,
        )
        await worker.recover()
        await worker.start()
        state.worker = worker
    try:
        yield
    finally:
        if state.worker is not None:
            await state.worker.stop()


def create_app(
    *,
    settings: ApiSettings | None = None,
    store: RunStore | None = None,
    blobs: BlobStore | None = None,
    worker: bool = True,
    execute: ExecuteFn = execute_run,
) -> FastAPI:
    """Build the API app; its lifespan owns the event hub and the job worker.

    Args:
        settings: API knobs; read from the environment when ``None``.
        store: The run store, kept on ``app.state.store``; built from the service settings at
            startup when ``None`` and the worker is enabled.
        blobs: The blob store, kept on ``app.state.blobs``; the RustFS landing bucket at
            startup when ``None`` and the worker is enabled.
        worker: Recover interrupted runs and start the in-process job worker
            (``app.state.worker``). Tests pass ``False``. The hub (``app.state.hub``) is
            created either way.
        execute: The execution core the worker calls (injectable for tests).
    """
    tracing.setup_tracing()  # no-op unless OTEL_EXPORTER_OTLP_ENDPOINT is set
    if settings is None:
        settings = ApiSettings()
    app = FastAPI(title="Sluice API", version="1", lifespan=_lifespan)
    app.state.settings = settings
    app.state.store = store
    app.state.blobs = blobs
    app.state.worker_enabled = worker
    app.state.execute = execute
    app.state.hub = None
    app.state.worker = None

    app.add_exception_handler(StarletteHTTPException, _http_exception)
    app.add_exception_handler(RequestValidationError, _request_validation)
    app.add_exception_handler(ApiError, _api_error)

    # Added first = inner: the 500 it produces still passes through CORS.
    app.add_middleware(InternalErrorMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.ui_origins),
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Last-Event-ID", "Content-Type"],
        allow_credentials=False,
    )

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        """Liveness: the process is up. Touches no dependency."""
        return {"status": "ok"}

    app.include_router(events_router)
    return app
