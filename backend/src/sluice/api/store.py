"""The API's run store in the Compose Postgres: runs, their events and their artefacts.

Tables (created by :meth:`RunStore.ensure_schema`, idempotent, under an advisory lock):

* ``api_runs``: one row per upload; ``status`` moves ``queued -> running -> <terminal>``.
* ``run_events``: every pipeline event of a run, keyed ``(run_id, seq)``; ``payload`` is the
  contract JSON (camelCase), parsed back with :data:`~sluice.agent.events.PIPELINE_EVENT_ADAPTER`,
  which covers all eleven contract event types (``activity`` included).
* ``run_artefacts``: the preview blob key and the transforms of a run (transforms are appended
  live, one per attempt that produced code; the preview key is set when the run has finished).

Connections: every method opens one short-lived connection and one short transaction (the API
is a single process with a concurrency of at most four runs, so a pool is not worth a new
dependency; connect cost is ~1-3 ms locally). Every transaction bounds its waits like
``sluice.load.iceberg``: transaction-local ``lock_timeout`` and ``statement_timeout``. All SQL
is static and parameterised.

The one long-lived connection is the worker lock's (:meth:`RunStore.try_worker_lock`): a
session-level advisory lock that lets only one API process per schema recover and run jobs.

Arguments are validated before any connection is opened, so bad input never reaches Postgres.
Methods are blocking; call them from a worker thread (``asyncio.to_thread``) in async code.
"""

import math
import re
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any, Final, LiteralString, get_args

import psycopg
from psycopg import errors
from psycopg.rows import TupleRow
from psycopg.types.json import Jsonb
from pydantic import Field

from sluice.agent.events import PIPELINE_EVENT_ADAPTER, EventModel, FinalStatus, TableName
from sluice.api.blobs import check_key
from sluice.api.schemas import ApiModel, RunStatus, RunSummary
from sluice.load.iceberg import advisory_key

PG_CONNECT_TIMEOUT_S: Final = 5
LOCK_TIMEOUT: Final = "5s"
STATEMENT_TIMEOUT: Final = "15s"
MAX_LIST_LIMIT: Final = 1000
MAX_FILE_NAME_CHARS: Final = 255
TERMINAL_STATUSES: Final = frozenset(get_args(FinalStatus))
_DDL_LOCK: Final = advisory_key("sluice.api.store.ddl")
WORKER_LOCK_NAME: Final = "sluice.api.worker"
_SET_CONFIG: Final = "SELECT set_config(%s, %s, true)"
_RUN_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_TABLES: Final = frozenset(get_args(TableName))

_DDL: Final = """
CREATE TABLE IF NOT EXISTS api_runs (
    id           text PRIMARY KEY,
    file_name    text NOT NULL,
    table_name   text NOT NULL,
    blob_key     text NOT NULL,
    file_sha256  text NOT NULL,
    rows         integer NULL,
    status       text NOT NULL
                 CHECK (status IN ('queued', 'running', 'succeeded', 'quarantined', 'failed')),
    cost_usd     double precision NOT NULL DEFAULT 0,
    created_at   timestamptz NOT NULL DEFAULT now(),
    started_at   timestamptz,
    finished_at  timestamptz
);
CREATE INDEX IF NOT EXISTS api_runs_status_created_at ON api_runs (status, created_at);
CREATE TABLE IF NOT EXISTS run_events (
    run_id   text REFERENCES api_runs (id),
    seq      integer NOT NULL CHECK (seq >= 1),
    at       timestamptz NOT NULL,
    type     text NOT NULL,
    payload  jsonb NOT NULL,
    PRIMARY KEY (run_id, seq)
);
CREATE TABLE IF NOT EXISTS run_artefacts (
    run_id       text PRIMARY KEY REFERENCES api_runs (id),
    preview_key  text,
    transforms   jsonb NOT NULL DEFAULT '[]',
    saved        boolean NOT NULL DEFAULT false
);
-- Before the column existed a row was only written by the end-of-run save: those are saved.
ALTER TABLE run_artefacts ADD COLUMN IF NOT EXISTS saved boolean NOT NULL DEFAULT true;
ALTER TABLE run_artefacts ALTER COLUMN saved SET DEFAULT false;
"""

# clock_timestamp(), not now(): runs created in one transaction still get distinct times.
_RUN_COLUMNS: Final = (
    "id, file_name, table_name, rows, status, created_at, cost_usd,"
    " blob_key, file_sha256, started_at, finished_at"
)
_INSERT_RUN: Final = (
    "INSERT INTO api_runs (id, file_name, table_name, blob_key, file_sha256, status, created_at)"
    " VALUES (%s, %s, %s, %s, %s, 'queued', clock_timestamp()) RETURNING " + _RUN_COLUMNS
)
_LIST_RUNS: Final = (
    "SELECT " + _RUN_COLUMNS + " FROM api_runs ORDER BY created_at DESC, id DESC LIMIT %s"
)
_GET_RUN: Final = "SELECT " + _RUN_COLUMNS + " FROM api_runs WHERE id = %s"
_CLAIM_NEXT: Final = (
    "UPDATE api_runs SET status = 'running', started_at = now() WHERE id = ("
    " SELECT id FROM api_runs WHERE status = 'queued'"
    " ORDER BY created_at, id FOR UPDATE SKIP LOCKED LIMIT 1"
    ") RETURNING " + _RUN_COLUMNS
)
_INTERRUPTED: Final = (
    "SELECT " + _RUN_COLUMNS + " FROM api_runs WHERE status = 'running' ORDER BY started_at, id"
)
_INSERT_EVENT: Final = (
    "INSERT INTO run_events (run_id, seq, at, type, payload)"
    " VALUES (%s, %s, %s::timestamptz, %s, %s::jsonb)"
)
_EVENTS_AFTER: Final = (
    "SELECT payload::text FROM run_events WHERE run_id = %s AND seq > %s ORDER BY seq"
)
_FINISH: Final = (
    "UPDATE api_runs SET status = %s, cost_usd = %s, finished_at = now()"
    " WHERE id = %s AND status IN ('queued', 'running')"
)
_EXISTS: Final = "SELECT 1 FROM api_runs WHERE id = %s"
# The end-of-run save: ``transforms`` win by ``transform_id``; a live-appended record whose id
# is not among them is kept (after them). Re-running it with the same arguments is a no-op.
_SAVE_ARTEFACTS: Final = (
    "INSERT INTO run_artefacts (run_id, preview_key, transforms, saved)"
    " VALUES (%s, %s, %s, true)"
    " ON CONFLICT (run_id) DO UPDATE"
    " SET preview_key = EXCLUDED.preview_key, saved = true,"
    " transforms = EXCLUDED.transforms || COALESCE(("
    "  SELECT jsonb_agg(kept.t ORDER BY kept.n)"
    "  FROM jsonb_array_elements(run_artefacts.transforms) WITH ORDINALITY AS kept(t, n)"
    "  WHERE NOT EXISTS ("
    "   SELECT 1 FROM jsonb_array_elements(EXCLUDED.transforms) AS given(t)"
    "   WHERE given.t->>'transform_id' = kept.t->>'transform_id')"
    " ), '[]'::jsonb)"
)
# One live transform: create the row if missing, else append unless its id is already there.
# The row lock taken by ON CONFLICT DO UPDATE serialises concurrent appends of one run.
_APPEND_TRANSFORM: Final = (
    "INSERT INTO run_artefacts (run_id, preview_key, transforms)"
    " VALUES (%s, NULL, jsonb_build_array(%s::jsonb))"
    " ON CONFLICT (run_id) DO UPDATE"
    " SET transforms = run_artefacts.transforms || EXCLUDED.transforms"
    " WHERE NOT run_artefacts.transforms @> jsonb_build_array("
    "  jsonb_build_object('transform_id', %s::text))"
)
TRANSFORM_KEYS: Final = frozenset({"transform_id", "node_id", "attempt", "code", "passed"})
"""Keys every transform record carries (``meta["transforms"]``, ``TransformRecord``)."""
_GET_ARTEFACTS: Final = "SELECT preview_key, transforms, saved FROM run_artefacts WHERE run_id = %s"


# --------------------------------------------------------------------------------------------
# Errors and models
# --------------------------------------------------------------------------------------------


class StoreError(Exception):
    """Base for run store errors."""


class RunNotFound(StoreError):
    """No ``api_runs`` row has this id."""


class DuplicateRun(StoreError):
    """A run with this id already exists."""


class DuplicateEvent(StoreError):
    """The run already has an event with this ``seq``."""


class RunNotActive(StoreError):
    """The run is already in a terminal status, so it cannot be finished again."""


class RunRow(RunSummary):
    """A run as stored: the public summary plus the server-side fields.

    Never return this from a route (it carries the blob key); use :meth:`summary`.
    """

    blob_key: str
    file_sha256: str
    started_at: str | None
    finished_at: str | None

    def summary(self) -> RunSummary:
        """The public :class:`RunSummary` (drops the server-side fields)."""
        return RunSummary.model_validate(self.model_dump(include=set(RunSummary.model_fields)))


class Artefacts(ApiModel):
    """What a run left behind for the preview and transforms routes.

    ``saved`` is set by the end-of-run :meth:`RunStore.save_artefacts`; until then the row
    holds only the transforms appended live, and ``preview_key`` is ``None`` (not final).
    """

    preview_key: str | None
    transforms: list[dict[str, Any]] = Field(default_factory=list)
    saved: bool = False


# --------------------------------------------------------------------------------------------
# Pure helpers
# --------------------------------------------------------------------------------------------


def iso_utc(moment: datetime) -> str:
    """``2026-10-27T10:14:03.123Z``: UTC, millisecond precision, ``Z`` suffix (as events)."""
    return moment.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def dump_event(event: EventModel) -> str:
    """The event as compact contract JSON (camelCase), as stored in ``run_events.payload``."""
    return event.to_json()


def parse_event(payload: str) -> EventModel:
    """Parse a stored payload into its concrete event model (discriminated on ``type``)."""
    return PIPELINE_EVENT_ADAPTER.validate_json(payload)


def _check_run_id(run_id: str) -> None:
    if not _RUN_ID.fullmatch(run_id):
        raise ValueError("run_id must match [A-Za-z0-9_-]{1,64}")


def _check_usd(cost_usd: float) -> None:
    if not math.isfinite(cost_usd) or cost_usd < 0:
        raise ValueError("cost_usd must be a finite, non-negative number")


def _opt_iso(moment: datetime | None) -> str | None:
    return None if moment is None else iso_utc(moment)


def _row(rec: TupleRow) -> RunRow:
    (id_, file_name, table, rows, status, created, cost, blob_key, sha, started, finished) = rec
    return RunRow(
        id=id_,
        file_name=file_name,
        table=table,
        rows=rows,
        status=status,
        created_at=iso_utc(created),
        cost_usd=cost,
        blob_key=blob_key,
        file_sha256=sha,
        started_at=_opt_iso(started),
        finished_at=_opt_iso(finished),
    )


# --------------------------------------------------------------------------------------------
# Store
# --------------------------------------------------------------------------------------------


class WorkerLock:
    """A held worker lock: a session advisory lock on its own open connection.

    Postgres releases it when the connection ends, so a crashed process never keeps it. If the
    connection drops while the process runs (Postgres restarted), the lock is gone silently;
    a second process could then take it (v1 runs one process, so this is only a safety net).
    """

    __slots__ = ("_conn", "_key")

    def __init__(self, conn: psycopg.Connection, key: int) -> None:
        self._conn = conn
        self._key = key

    def release(self) -> None:
        """Unlock and close the connection (best effort: closing alone releases it too)."""
        try:
            if not self._conn.closed:
                self._conn.execute("SELECT pg_advisory_unlock(%s)", (self._key,))
        except psycopg.Error:
            pass
        finally:
            self._conn.close()


class RunStore:
    """Postgres-backed store of API runs. Thread-safe: no state is shared between calls."""

    __slots__ = ("_dsn",)

    def __init__(self, dsn: str) -> None:
        """``dsn`` is a libpq URL; it holds the password, so it is never logged or shown."""
        self._dsn = dsn

    def __repr__(self) -> str:
        return "RunStore(dsn=<hidden>)"

    @contextmanager
    def _tx(self) -> Iterator[psycopg.Connection]:
        """One connection, one transaction, with bounded lock and statement waits."""
        with (
            psycopg.connect(self._dsn, connect_timeout=PG_CONNECT_TIMEOUT_S) as conn,
            conn.transaction(),
        ):
            conn.execute(_SET_CONFIG, ("lock_timeout", LOCK_TIMEOUT))
            conn.execute(_SET_CONFIG, ("statement_timeout", STATEMENT_TIMEOUT))
            yield conn

    def _execute(self, sql: LiteralString, params: tuple[Any, ...]) -> list[TupleRow]:
        with self._tx() as conn:
            cur = conn.execute(sql, params)
            return cur.fetchall() if cur.description else []

    def try_worker_lock(self) -> WorkerLock | None:
        """Take the job-worker lock of this store's schema, or ``None`` if another holds it.

        Non-blocking (``pg_try_advisory_lock``). The key is derived from
        :data:`WORKER_LOCK_NAME` and ``current_schema()``, so isolated test schemas never
        contend with each other or with a dev API. The returned handle keeps its connection
        open until :meth:`WorkerLock.release`.
        """
        conn = psycopg.connect(self._dsn, connect_timeout=PG_CONNECT_TIMEOUT_S, autocommit=True)
        try:
            schema_row = conn.execute("SELECT current_schema()").fetchone()
            key = advisory_key(WORKER_LOCK_NAME, str(schema_row[0] if schema_row else ""))
            got = conn.execute("SELECT pg_try_advisory_lock(%s)", (key,)).fetchone()
        except BaseException:
            conn.close()
            raise
        if not (got and got[0]):
            conn.close()
            return None
        return WorkerLock(conn, key)

    def ensure_schema(self) -> None:
        """Create the tables and index if missing; concurrent callers are serialised."""
        with self._tx() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (_DDL_LOCK,))
            conn.execute(_DDL)

    def create_run(
        self, *, run_id: str, file_name: str, table: str, blob_key: str, file_sha256: str
    ) -> RunSummary:
        """Insert a ``queued`` run. Raises :class:`DuplicateRun` if the id exists."""
        _check_run_id(run_id)
        if not 1 <= len(file_name) <= MAX_FILE_NAME_CHARS:
            raise ValueError("file_name must be 1..255 characters")
        if table not in _TABLES:
            raise ValueError("table must be one of the contract tables")
        check_key(blob_key)
        if not _SHA256.fullmatch(file_sha256):
            raise ValueError("file_sha256 must be 64 lowercase hex characters")
        try:
            (rec,) = self._execute(_INSERT_RUN, (run_id, file_name, table, blob_key, file_sha256))
        except errors.UniqueViolation as exc:
            raise DuplicateRun(run_id) from exc
        return _row(rec).summary()

    def list_runs(self, limit: int = 100) -> list[RunSummary]:
        """At most ``limit`` (1..1000) runs, newest first."""
        if not 1 <= limit <= MAX_LIST_LIMIT:
            raise ValueError("limit must be between 1 and 1000")
        return [_row(rec).summary() for rec in self._execute(_LIST_RUNS, (limit,))]

    def get_run(self, run_id: str) -> RunRow | None:
        """The stored run, or ``None`` if unknown (including ids that are not valid ids)."""
        if not _RUN_ID.fullmatch(run_id):
            return None
        recs = self._execute(_GET_RUN, (run_id,))
        return _row(recs[0]) if recs else None

    def claim_next(self) -> RunRow | None:
        """Atomically move the oldest ``queued`` run to ``running`` and return it.

        ``FOR UPDATE SKIP LOCKED``: concurrent claimers never get the same row, nor block on
        each other. Uses the ``(status, created_at)`` index.
        """
        recs = self._execute(_CLAIM_NEXT, ())
        return _row(recs[0]) if recs else None

    def interrupted(self) -> list[RunRow]:
        """Runs left ``running`` (by a crash or restart), oldest first, for startup recovery."""
        return [_row(rec) for rec in self._execute(_INTERRUPTED, ())]

    def append_event(self, run_id: str, event: EventModel) -> None:
        """Store a stamped event. Raises :class:`DuplicateEvent` if ``seq`` is taken and
        :class:`RunNotFound` if the run does not exist."""
        _check_run_id(run_id)
        if event.seq < 1 or not event.at:
            raise ValueError("event must be stamped (seq >= 1 and at) before it is stored")
        event_type: str = event.type  # type: ignore[attr-defined]
        try:
            self._execute(
                _INSERT_EVENT, (run_id, event.seq, event.at, event_type, dump_event(event))
            )
        except errors.UniqueViolation as exc:
            raise DuplicateEvent(f"{run_id} seq {event.seq}") from exc
        except errors.ForeignKeyViolation as exc:
            raise RunNotFound(run_id) from exc

    def events_after(self, run_id: str, after_seq: int) -> list[EventModel]:
        """The run's events with ``seq > after_seq``, in ``seq`` order (``0`` = all)."""
        if after_seq < 0:
            raise ValueError("after_seq must be >= 0")
        if not _RUN_ID.fullmatch(run_id):
            return []
        recs = self._execute(_EVENTS_AFTER, (run_id, after_seq))
        return [parse_event(payload) for (payload,) in recs]

    def update_progress(
        self, run_id: str, *, rows: int | None = None, cost_usd: float | None = None
    ) -> None:
        """Set the row count and/or running cost; a call with neither does nothing."""
        if rows is not None and rows < 0:
            raise ValueError("rows must be >= 0")
        if cost_usd is not None:
            _check_usd(cost_usd)
        if rows is None and cost_usd is None:
            return
        _check_run_id(run_id)
        with self._tx() as conn:
            cur = conn.execute(
                "UPDATE api_runs SET rows = COALESCE(%s, rows), cost_usd = COALESCE(%s, cost_usd)"
                " WHERE id = %s",
                (rows, cost_usd, run_id),
            )
            if cur.rowcount == 0:
                raise RunNotFound(run_id)

    def finish(self, run_id: str, status: RunStatus, cost_usd: float) -> None:
        """Move a ``queued``/``running`` run to a terminal status with its final cost.

        Raises ``ValueError`` for a non-terminal ``status``, :class:`RunNotActive` if the run
        has already finished (its terminal status never changes) and :class:`RunNotFound`.
        """
        if status not in TERMINAL_STATUSES:
            raise ValueError("status must be terminal: succeeded, quarantined or failed")
        _check_usd(cost_usd)
        _check_run_id(run_id)
        with self._tx() as conn:
            if conn.execute(_FINISH, (status, cost_usd, run_id)).rowcount == 1:
                return
            if conn.execute(_EXISTS, (run_id,)).fetchone() is None:
                raise RunNotFound(run_id)
            raise RunNotActive(run_id)

    def append_transform(self, run_id: str, record: dict[str, Any]) -> None:
        """Append one transform record while the run is live (one atomic statement).

        Creates the ``run_artefacts`` row (no preview yet) when missing. Idempotent: a record
        whose ``transform_id`` is already stored is ignored. Raises ``ValueError`` for a record
        missing a :data:`TRANSFORM_KEYS` key or with a malformed ``transform_id``, and
        :class:`RunNotFound`.
        """
        _check_run_id(run_id)
        missing = TRANSFORM_KEYS - record.keys()
        if missing:
            raise ValueError(f"transform record lacks {sorted(missing)}")
        tid = record["transform_id"]
        if not isinstance(tid, str) or not _RUN_ID.fullmatch(tid):
            raise ValueError("transform_id must match [A-Za-z0-9_-]{1,64}")
        try:
            self._execute(_APPEND_TRANSFORM, (run_id, Jsonb(record), tid))
        except errors.ForeignKeyViolation as exc:
            raise RunNotFound(run_id) from exc

    def save_artefacts(
        self, run_id: str, *, preview_key: str | None, transforms: list[dict[str, Any]]
    ) -> None:
        """Set the preview key and reconcile the transforms at the end of a run.

        ``transforms`` replace stored records with the same ``transform_id``; stored records
        whose id is not among them (appended live by :meth:`append_transform`) are kept, so
        nothing is dropped or duplicated. Marks the artefacts ``saved``. Idempotent.
        """
        _check_run_id(run_id)
        try:
            self._execute(_SAVE_ARTEFACTS, (run_id, preview_key, Jsonb(transforms)))
        except errors.ForeignKeyViolation as exc:
            raise RunNotFound(run_id) from exc

    def get_artefacts(self, run_id: str) -> Artefacts | None:
        """The run's artefacts, or ``None`` if none were saved."""
        if not _RUN_ID.fullmatch(run_id):
            return None
        recs = self._execute(_GET_ARTEFACTS, (run_id,))
        if not recs:
            return None
        preview_key, transforms, saved = recs[0]
        return Artefacts(preview_key=preview_key, transforms=transforms, saved=saved)
