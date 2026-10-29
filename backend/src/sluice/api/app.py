"""FastAPI application factory: contract error bodies, CORS on every response, health check.

Every 4xx/5xx body is ``{"detail": str, "code"?: str}`` (``docs/api/contract.md``). Middleware
order, outermost first::

    ServerErrorMiddleware (Starlette) -> CORSMiddleware -> InternalErrorMiddleware -> routes

An ``exception_handler(Exception)`` would run in Starlette's outermost ``ServerErrorMiddleware``,
outside CORS, so the browser would see a 500 without ``Access-Control-Allow-Origin`` and report a
network error. ``InternalErrorMiddleware`` sits *inside* CORS instead, so the generic 500 carries
the CORS headers like every other response.
"""

import logging
from typing import Any, Final

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from sluice.api.schemas import ErrorBody
from sluice.api.settings import ApiSettings

logger = logging.getLogger(__name__)

INTERNAL_DETAIL: Final = "Internal error."
MAX_DETAIL_CHARS: Final = 500
"""Longest validation ``detail``; FastAPI messages are short, this only bounds pathological ones."""

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


class InternalErrorMiddleware:
    """Turns an unhandled exception into the generic contract 500, inside CORS.

    The exception is logged with its traceback server-side; the body never contains it. If the
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
        except Exception:
            logger.exception("unhandled error in %s %s", scope.get("method"), scope.get("path"))
            if started:
                raise
            await error_response(500, INTERNAL_DETAIL, "internal")(scope, receive, send)


def create_app(
    *,
    settings: ApiSettings | None = None,
    store: Any | None = None,
    blobs: Any | None = None,
    worker: bool = True,
) -> FastAPI:
    """Build the API app. Later tasks register their routers and lifespan here.

    Args:
        settings: API knobs; read from the environment when ``None``.
        store: The run store (typed in Task 2); kept on ``app.state.store``.
        blobs: The blob store (typed in Task 2); kept on ``app.state.blobs``.
        worker: Start the in-process job worker. Tests pass ``False``.
    """
    settings = settings or ApiSettings()
    app = FastAPI(title="Sluice API", version="1")
    app.state.settings = settings
    app.state.store = store
    app.state.blobs = blobs
    app.state.worker_enabled = worker

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

    return app
