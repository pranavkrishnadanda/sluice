"""Run routes: ``POST /runs`` (streamed upload), ``GET /runs``, transforms and the preview.

Upload (``docs/api/contract.md``, "Upload"):

* The multipart body is parsed **incrementally** from ``request.stream()`` with
  python-multipart's streaming parser (:class:`MultipartStream`); Starlette's ``request.form()``
  is never called, so nothing spools the body. File bytes go straight to
  :meth:`BlobStore.put_stream`, which stops on the first chunk past the limit. Every body byte
  also counts against ``limit + MULTIPART_OVERHEAD``, so bytes outside the file part cannot
  bypass the cap; a ``Content-Length`` over that is refused before reading anything. At most
  the limit plus one received chunk is read. Memory is O(one chunk).
* The blob key is generated (``uploads/<run_id>/input<ext>``); the client's name is only
  displayed (:func:`sanitise_file_name`) and never touches a path.
* Order: ``unknown table`` is known as soon as the ``table`` field arrives (before the file when
  the client sends it first); the extension is checked on the file part's headers, before any
  of its bytes are stored. After the stream: parse check (``read_input``) and SHA-256 in a
  thread, then the row is inserted and the worker is woken. Any failure after the blob was
  written deletes it.

Store and blob calls are blocking and run in ``asyncio.to_thread``.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import logging
import unicodedata
import uuid
from collections import deque
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Final, get_args

import polars as pl
from fastapi import APIRouter, Query, Request
from python_multipart.exceptions import FormParserError
from python_multipart.multipart import MultipartParser, parse_options_header

from sluice.agent import events as ev
from sluice.agent.events import TableName
from sluice.api.blobs import BlobStore, TooLarge
from sluice.api.errors import ApiError
from sluice.api.schemas import DataPreview, RunSummary, Transform
from sluice.api.store import MAX_FILE_NAME_CHARS, TERMINAL_STATUSES, RunRow, RunStore
from sluice.contracts.model import Contract, load_contract
from sluice.io import read_input
from sluice.runs.execute import file_sha256
from sluice.runs.preview import PREVIEW_MAX_LIMIT, page_preview

_log = logging.getLogger(__name__)

router = APIRouter()

ALLOWED_EXTENSIONS: Final = (".csv", ".json", ".parquet")
TABLES: Final = frozenset(get_args(TableName))
LIST_LIMIT: Final = 100
MAX_FIELD_BYTES: Final = 1024
"""Longest accepted ``table`` field value (the real values are 6-8 bytes)."""
MAX_PARTS: Final = 16
MULTIPART_OVERHEAD: Final = 64 * 1024
"""Body bytes allowed on top of the file limit: boundaries, part headers, the ``table`` field."""
MAX_OFFSET: Final = 2**31 - 1
TOO_LARGE_DETAIL: Final = "File too large: the limit is 100 MB."
UNSUPPORTED_DETAIL: Final = "Unsupported file format: upload a .csv, .json or .parquet file."
UNKNOWN_TABLE_DETAIL: Final = "Unknown table: choose orders or lineitem."
UNPARSEABLE_DETAIL: Final = "The file could not be read in its format."
RUN_NOT_FOUND: Final = "Run not found."
NOT_LOADED_DETAIL: Final = "The preview is available once the run has loaded its data."
NOT_READY_DETAIL: Final = "The preview is not ready yet."
NO_PREVIEW_DETAIL: Final = "This run has no preview."
PREVIEW_SAVE_GRACE_S: Final = 300.0
"""How long after ``finished_at`` an unsaved preview still counts as ``not_ready``.

The worker finishes a run (and publishes ``run_finished``) before it builds the preview, so a
terminal run normally answers ``not_ready`` until the end-of-run save marks its artefacts
``saved``. If the process died in between, the save never comes: past this grace the answer
is ``no_preview``. Generous: building the preview of a 100 MB upload takes seconds.
"""


class BadForm(Exception):
    """The multipart body is malformed or misses a required field (422 ``invalid_request``)."""


# --------------------------------------------------------------------------------------------
# File names
# --------------------------------------------------------------------------------------------


def upload_extension(name: str) -> str | None:
    """The accepted extension of ``name`` (lower-case), or ``None``. ASCII match only."""
    lowered = name.lower()
    return next((ext for ext in ALLOWED_EXTENSIONS if lowered.endswith(ext)), None)


def sanitise_file_name(raw: str, ext: str) -> str:
    """The display name: basename only, no control characters, NFC, at most 255 characters.

    Both ``/`` and ``\\`` separate path components (browsers on Windows may send either). An
    over-long name is cut in its stem so the extension survives. Empty -> ``"upload<ext>"``.
    """
    name = raw.replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(ch for ch in name if unicodedata.category(ch) != "Cc")
    name = unicodedata.normalize("NFC", name).strip()
    if name in ("", ".", ".."):
        return f"upload{ext}"
    if len(name) > MAX_FILE_NAME_CHARS:
        suffix = name[-len(ext) :] if name.lower().endswith(ext) else ""
        name = name[: MAX_FILE_NAME_CHARS - len(suffix)] + suffix
    return name


# --------------------------------------------------------------------------------------------
# Streaming multipart
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FieldEvent:
    """A complete ``table`` field."""

    value: bytes


@dataclass(frozen=True, slots=True)
class FileStart:
    """The ``file`` part's headers have arrived."""

    filename: str


@dataclass(frozen=True, slots=True)
class FileData:
    data: bytes


@dataclass(frozen=True, slots=True)
class FileEnd:
    pass


Event = FieldEvent | FileStart | FileData | FileEnd


class MultipartStream:
    """Pull-based multipart parsing: one received chunk is parsed only when an event is needed.

    Emits :class:`FieldEvent` for the ``table`` field, and :class:`FileStart` /
    :class:`FileData` / :class:`FileEnd` for the first part named ``file`` that has a
    ``filename``. Other parts are skipped (their bytes still count against ``max_total``).

    Raises (from :meth:`next`): :class:`TooLarge` once more than ``max_total`` body bytes have
    arrived, :class:`BadForm` for malformed bodies, a second ``file`` part, an over-long
    ``table`` value, too many parts or a body that ends mid-way.
    """

    def __init__(self, chunks: AsyncIterator[bytes], boundary: bytes, max_total: int) -> None:
        self._chunks = chunks
        self._max_total = max_total
        self._total = 0
        self._pending: deque[Event] = deque()
        self._exhausted = False
        self._ended = False
        self._parts = 0
        self._seen_file = False
        self._kind: str | None = None  # "table" | "file" | None (skipped) for the current part
        self._field = bytearray()
        self._header_name = bytearray()
        self._header_value = bytearray()
        self._disposition = b""
        self._parser = MultipartParser(
            boundary,
            {
                "on_part_begin": self._on_part_begin,
                "on_part_data": self._on_part_data,
                "on_part_end": self._on_part_end,
                "on_header_field": self._on_header_field,
                "on_header_value": self._on_header_value,
                "on_header_end": self._on_header_end,
                "on_headers_finished": self._on_headers_finished,
                "on_end": self._on_end,
            },
        )

    async def next(self) -> Event | None:
        """The next event, reading more of the body only when none is pending; ``None`` at end."""
        while not self._pending:
            if self._exhausted:
                if not self._ended:
                    raise BadForm("the multipart body ended before its closing boundary")
                return None
            chunk = await anext(self._chunks, None)
            if chunk is None:
                self._exhausted = True
                self._parser.finalize()
                continue
            self._total += len(chunk)
            if self._total > self._max_total:
                raise TooLarge(self._max_total)
            try:
                self._parser.write(chunk)
            except FormParserError as exc:
                raise BadForm("malformed multipart body") from exc
        return self._pending.popleft()

    # parser callbacks (synchronous, called from ``write``) ----------------------------------

    def _on_part_begin(self) -> None:
        self._parts += 1
        if self._parts > MAX_PARTS:
            raise BadForm("too many multipart parts")
        self._kind = None
        self._disposition = b""
        self._field.clear()

    def _on_header_field(self, data: bytes, start: int, end: int) -> None:
        self._header_name += data[start:end]

    def _on_header_value(self, data: bytes, start: int, end: int) -> None:
        self._header_value += data[start:end]

    def _on_header_end(self) -> None:
        if bytes(self._header_name).lower() == b"content-disposition":
            self._disposition = bytes(self._header_value)
        self._header_name.clear()
        self._header_value.clear()

    def _on_headers_finished(self) -> None:
        _, options = parse_options_header(self._disposition)
        name = options.get(b"name")
        if name is None:
            raise BadForm('a multipart part has no Content-Disposition "name"')
        filename = options.get(b"filename")
        if name == b"file" and filename is not None:
            if self._seen_file:
                raise BadForm("only one file may be uploaded")
            self._seen_file = True
            self._kind = "file"
            self._pending.append(FileStart(filename.decode("utf-8", errors="replace")))
        elif name == b"table" and filename is None:
            self._kind = "table"

    def _on_part_data(self, data: bytes, start: int, end: int) -> None:
        if self._kind == "file":
            self._pending.append(FileData(data[start:end]))
        elif self._kind == "table":
            if len(self._field) + (end - start) > MAX_FIELD_BYTES:
                raise BadForm("the table field is too long")
            self._field += data[start:end]

    def _on_part_end(self) -> None:
        if self._kind == "file":
            self._pending.append(FileEnd())
        elif self._kind == "table":
            self._pending.append(FieldEvent(bytes(self._field)))
        self._kind = None

    def _on_end(self) -> None:
        self._ended = True


async def _file_chunks(form: MultipartStream) -> AsyncIterator[bytes]:
    """The current file part's bytes, until its :class:`FileEnd`."""
    while True:
        event = await form.next()
        if isinstance(event, FileData):
            yield event.data
        elif isinstance(event, FileEnd):
            return
        else:  # None (truncated) cannot happen: next() raises BadForm first
            raise BadForm("the file part is incomplete")


def _boundary(content_type: str | None) -> bytes:
    if not content_type:
        raise BadForm("the body must be multipart/form-data")
    kind, params = parse_options_header(content_type)
    boundary = params.get(b"boundary")
    if kind != b"multipart/form-data" or not boundary:
        raise BadForm("the body must be multipart/form-data with a boundary")
    return boundary


def _content_length(headers: Mapping[str, str]) -> int | None:
    raw = headers.get("content-length")
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError as exc:
        raise BadForm("invalid Content-Length") from exc


def _table(value: bytes | None) -> str:
    if value is None:
        raise BadForm('the form field "table" is required')
    try:
        table = value.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise BadForm('the form field "table" is not valid UTF-8') from exc
    if table not in TABLES:
        raise ApiError(422, UNKNOWN_TABLE_DETAIL, "unknown_table")
    return table


@dataclass(frozen=True, slots=True)
class _Upload:
    table: str
    file_name: str
    key: str


async def _receive(
    form: MultipartStream, run_id: str, blobs: BlobStore, max_bytes: int, written: list[str]
) -> _Upload:
    """Consume the body: validate fields as they come, stream the file into the blob store."""
    table: bytes | None = None
    upload: tuple[str, str] | None = None  # (file_name, key)
    while (event := await form.next()) is not None:
        if isinstance(event, FieldEvent):
            if table is not None:
                raise BadForm('the form field "table" was sent twice')
            table = event.value
            _table(table)  # fail fast, before reading the file
        elif isinstance(event, FileStart):
            ext = upload_extension(event.filename)
            if ext is None:
                raise ApiError(415, UNSUPPORTED_DETAIL, "unsupported_format")
            key = f"uploads/{run_id}/input{ext}"
            written.append(key)
            await blobs.put_stream(key, _file_chunks(form), max_bytes=max_bytes)
            upload = (sanitise_file_name(event.filename, ext), key)
    if upload is None:
        raise BadForm('the form field "file" is required')
    return _Upload(table=_table(table), file_name=upload[0], key=upload[1])


def _check_and_hash(blobs: BlobStore, key: str) -> str:
    """Parse-check the stored upload and return its SHA-256 (blocking)."""
    with blobs.open_local(key) as path:
        try:
            read_input(path)
        except MemoryError:
            raise
        except Exception as exc:  # polars, decoding and shape errors alike mean "unparseable"
            _log.info("upload %s is unparseable: %s", key, type(exc).__name__)
            raise ApiError(422, UNPARSEABLE_DETAIL, "unparseable") from None
        return file_sha256(path)


def _delete_quietly(blobs: BlobStore, keys: list[str]) -> None:
    for key in keys:
        try:
            blobs.delete(key)
        except Exception as exc:  # best effort: the caller is already failing
            _log.error("deleting upload %s failed: %s", key, type(exc).__name__)


@router.post("/runs", status_code=201, response_model=RunSummary)
async def create_run(request: Request) -> RunSummary:
    """Accept a multipart upload (``file``, ``table``) and queue a run for it."""
    state = request.app.state
    store: RunStore = state.store
    blobs: BlobStore = state.blobs
    limit: int = state.settings.max_upload_bytes
    max_total = limit + MULTIPART_OVERHEAD
    written: list[str] = []
    try:
        boundary = _boundary(request.headers.get("content-type"))
        length = _content_length(request.headers)
        if length is not None and length > max_total:
            raise TooLarge(max_total)
        run_id = uuid.uuid4().hex
        form = MultipartStream(request.stream(), boundary, max_total)
        upload = await _receive(form, run_id, blobs, limit, written)
        sha = await asyncio.to_thread(_check_and_hash, blobs, upload.key)
        summary = await asyncio.to_thread(
            store.create_run,
            run_id=run_id,
            file_name=upload.file_name,
            table=upload.table,
            blob_key=upload.key,
            file_sha256=sha,
        )
    except BaseException as exc:
        if written:
            with contextlib.suppress(Exception):
                await asyncio.to_thread(_delete_quietly, blobs, written)
        if isinstance(exc, TooLarge):
            raise ApiError(413, TOO_LARGE_DETAIL, "too_large") from None
        if isinstance(exc, BadForm):
            raise ApiError(422, f"Invalid upload: {exc}.", "invalid_request") from None
        raise
    worker = state.worker
    if worker is not None:
        worker.notify()
    return summary


# --------------------------------------------------------------------------------------------
# Reads
# --------------------------------------------------------------------------------------------


@router.get("/runs", response_model=list[RunSummary])
async def list_runs(request: Request) -> list[RunSummary]:
    """The newest :data:`LIST_LIMIT` runs, newest first."""
    store: RunStore = request.app.state.store
    return await asyncio.to_thread(store.list_runs, LIST_LIMIT)


def _run_or_404(store: RunStore, run_id: str) -> RunRow:
    row = store.get_run(run_id)
    if row is None:
        raise ApiError(404, RUN_NOT_FOUND, "not_found")
    return row


def _plan_order(events: list[Any]) -> dict[str, int]:
    for event in events:
        if isinstance(event, ev.PlanCreated):
            return {node.id: i for i, node in enumerate(event.nodes)}
    return {}


def _transforms(store: RunStore, run_id: str) -> list[Transform]:
    _run_or_404(store, run_id)
    artefacts = store.get_artefacts(run_id)
    if artefacts is None or not artefacts.transforms:
        return []
    order = _plan_order(store.events_after(run_id, 0))
    rows = sorted(
        artefacts.transforms,
        key=lambda t: (order.get(t.get("node_id"), len(order)), t.get("attempt", 0)),
    )
    return [
        Transform(
            id=t["transform_id"],
            node_id=t["node_id"],
            attempt=t["attempt"],
            language="polars",
            code=t["code"],
            passed=bool(t["passed"]),
        )
        for t in rows
    ]


@router.get("/runs/{run_id}/transforms", response_model=list[Transform])
async def get_transforms(request: Request, run_id: str) -> list[Transform]:
    """One transform per attempt that produced code: plan order, then attempt."""
    return await asyncio.to_thread(_transforms, request.app.state.store, run_id)


@functools.cache
def _contract(table: str) -> Contract:
    return load_contract(table)


def _read_page(path: Path, contract: Contract, offset: int, limit: int) -> DataPreview:
    """One page of the stored preview Parquet, reading only that page's rows."""
    lazy = pl.scan_parquet(path)
    total = int(lazy.select(pl.len()).collect().item())
    frame = lazy.slice(offset, limit).collect()
    page = page_preview(frame, contract, offset=0, limit=limit)
    end = offset + limit
    return page.model_copy(
        update={"total_rows": total, "next_offset": end if end < total else None}
    )


def _save_overdue(row: RunRow) -> bool:
    """Whether a finished run's end-of-run save is :data:`PREVIEW_SAVE_GRACE_S` overdue."""
    if row.finished_at is None:
        return False
    finished = datetime.fromisoformat(row.finished_at)
    return (datetime.now(UTC) - finished).total_seconds() > PREVIEW_SAVE_GRACE_S


def _preview(
    store: RunStore, blobs: BlobStore, run_id: str, offset: int, limit: int
) -> DataPreview:
    row = _run_or_404(store, run_id)
    if not any(isinstance(e, ev.Loaded) for e in store.events_after(run_id, 0)):
        raise ApiError(409, NOT_LOADED_DETAIL, "not_loaded")
    artefacts = store.get_artefacts(run_id)
    key = None if artefacts is None else artefacts.preview_key
    if key is None:
        saved = artefacts is not None and artefacts.saved
        if row.status in TERMINAL_STATUSES and (saved or _save_overdue(row)):
            raise ApiError(409, NO_PREVIEW_DETAIL, "no_preview")
        raise ApiError(409, NOT_READY_DETAIL, "not_ready")
    with blobs.open_local(key) as path:
        return _read_page(path, _contract(row.table), offset, min(limit, PREVIEW_MAX_LIMIT))


@router.get("/runs/{run_id}/preview", response_model=DataPreview)
async def get_preview(
    request: Request,
    run_id: str,
    offset: Annotated[int, Query(ge=0, le=MAX_OFFSET)] = 0,
    limit: Annotated[int, Query(ge=1)] = PREVIEW_MAX_LIMIT,
) -> DataPreview:
    """A page of the before/after preview; ``409`` until the run has emitted ``loaded``."""
    state = request.app.state
    return await asyncio.to_thread(_preview, state.store, state.blobs, run_id, offset, limit)
