"""Load a :class:`~sluice.evals.types.SystemOutput` into Iceberg and record lineage in Postgres.

Storage layout
--------------
* Iceberg SQL catalog in the Compose Postgres, warehouse ``s3://<warehouse_bucket>/`` on RustFS.
* ``<namespace>.<table>``: the contract schema. Key columns are ``required`` (and the Iceberg
  identifier fields); every other column is ``optional`` because flagged cells are null.
* ``<namespace>.<table>_quarantine``: ``pk`` (the key as compact JSON text, e.g. ``[1]``),
  ``reason``, ``run_id``, ``file_sha256``.
* Postgres lineage tables (created ``IF NOT EXISTS``): ``runs``, ``plan_nodes``, ``transforms``,
  ``loads`` and ``flags``. Flags live in the ``flags`` table (one row per flagged cell), not in a
  JSON column, so they can be joined and counted.

Type mapping (contract dtype -> Iceberg -> Arrow)
-------------------------------------------------
``int64`` -> ``long`` -> ``int64``; ``int32`` -> ``int`` -> ``int32``; ``decimal`` ->
``decimal(15, 2)`` -> ``decimal128(15, 2)``; ``string`` -> ``string`` -> ``large_string``;
``date`` -> ``date`` -> ``date32``; ``timestamp_utc`` -> ``timestamptz`` (microseconds) ->
``timestamp[us, tz=UTC]``. The frame must already have the exact contract Polars schema; nothing
is coerced through floats, and the final Arrow cast is a safe (overflow-checked) cast.

Idempotency
-----------
The key is ``(iceberg_table, file_sha256, contract.version)``, unique in ``loads`` (Ruling L1:
the same bytes loaded into another table are a different load). A load runs inside one Postgres
transaction holding two advisory locks (the target table, then the key), so concurrent loads of
a table are serialised and two loads of one file cannot race. Every Iceberg append carries the
snapshot summary properties ``sluice.file_sha256`` / ``sluice.contract_version`` /
``sluice.run_id``; summaries are per table, so the snapshot check below is keyed by table too.
:func:`find_load` is a lock-free read of the same key, used by ``sluice run`` to skip the
pipeline for a repeat file; the in-transaction check stays authoritative (race safety).

Timeouts: every lineage transaction sets, transaction-locally, ``lock_timeout = 10s`` and
``statement_timeout = 15s`` while it waits for the DDL/advisory locks, then raises
``statement_timeout`` to 60s for the remaining statements. A load stuck behind another loader
fails with a ``psycopg`` lock/cancel error instead of hanging. (Iceberg's own writes to S3 run
between statements, so they are bounded by the S3 timeouts, not by ``statement_timeout``.)

Order of work:

1. ``loads`` already has the key -> return ``skipped=True``; nothing is written.
2. Otherwise look for an append with this key among the *ancestors of the current snapshot* of
   each target table (the main table and the quarantine table independently). A match means an
   earlier attempt committed to Iceberg and crashed before recording; that table is not appended
   again (``recovered=True``).
3. Append what is missing, then insert ``runs``/``loads``/``flags``/``plan_nodes``/
   ``transforms`` and commit.

Only a load that gave the main table a snapshot claims the key (Ruling F1). A whole-file
quarantine writes nothing to Iceberg and records only its ``runs`` row (and lineage); an output
with no main-table rows (every row quarantined) appends its quarantine rows and records its run
but no ``loads`` row. Either file can therefore be run again; a retry of the second does not
duplicate its quarantine rows (step 2). A ``loads`` row with a NULL snapshot (written before
F1) is ignored by the lookup and deleted when the key is next recorded. ``failed`` runs are
never loaded (:func:`load_status`).

So a crash anywhere between an Iceberg commit and the Postgres commit is healed by the retry
without duplicate rows. :func:`rollback` deletes the ``loads`` rows of the snapshots it rolls
back (and marks their runs ``rolled_back``), so a rolled-back file can be loaded again; since
only ancestors are searched in step 2, the orphaned snapshot does not block that reload.

Cost: one scan of the table's snapshot ancestry per load (O(snapshots)), one Arrow conversion
(O(rows), zero-copy for most columns) and one Postgres round trip per lineage table.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import re
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

import polars as pl
import psycopg
import pyarrow as pa
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from pyiceberg.catalog.sql import SqlCatalog
from pyiceberg.exceptions import NoSuchTableError
from pyiceberg.schema import Schema
from pyiceberg.table import Table
from pyiceberg.table.snapshots import ancestors_of
from pyiceberg.types import (
    DateType,
    DecimalType,
    IcebergType,
    IntegerType,
    LongType,
    NestedField,
    StringType,
    TimestamptzType,
)
from sqlalchemy.exc import IntegrityError

from sluice.agent.events import ModelTier, OpaqueId
from sluice.contracts.model import MONEY_PRECISION, MONEY_SCALE, Contract, Dtype
from sluice.evals.types import PK, SystemOutput
from sluice.settings import Settings

NAMESPACE = "sluice"
CATALOG_NAME = "sluice"
QUARANTINE_SUFFIX = "_quarantine"
PROP_FILE_SHA256 = "sluice.file_sha256"
PROP_CONTRACT_VERSION = "sluice.contract_version"
PROP_RUN_ID = "sluice.run_id"
S3_REGION = "us-east-1"
S3_TIMEOUT_S = 30
PG_CONNECT_TIMEOUT_S = 5
STATUS_SUCCEEDED = "succeeded"
STATUS_QUARANTINED = "quarantined"
STATUS_ROLLED_BACK = "rolled_back"
LOADABLE_STATUSES = frozenset({STATUS_SUCCEEDED, STATUS_QUARANTINED})
"""Run statuses an output may be loaded with; a ``failed`` run is never loaded (Ruling F1)."""
DEFAULT_FK_REASON = "foreign_key"
DEFAULT_REASON = "rejected"
MAX_CODE_CHARS = 100_000
LOCK_TIMEOUT = "10s"
LOCK_WAIT_STATEMENT_TIMEOUT = "15s"
STATEMENT_TIMEOUT = "60s"
_SET_CONFIG = "SELECT set_config(%s, %s, true)"

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_RUN_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_IDENT = re.compile(r"^[a-z][a-z0-9_]{0,62}$")

_ICEBERG_TYPES: dict[str, IcebergType] = {
    "int64": LongType(),
    "int32": IntegerType(),
    "decimal": DecimalType(MONEY_PRECISION, MONEY_SCALE),
    "string": StringType(),
    "date": DateType(),
    "timestamp_utc": TimestamptzType(),
}
_ARROW_TYPES: dict[str, pa.DataType] = {
    "int64": pa.int64(),
    "int32": pa.int32(),
    "decimal": pa.decimal128(MONEY_PRECISION, MONEY_SCALE),
    "string": pa.large_string(),
    "date": pa.date32(),
    "timestamp_utc": pa.timestamp("us", tz="UTC"),
}

_QUARANTINE_FIELDS = (
    NestedField(1, "pk", StringType(), required=True),
    NestedField(2, "reason", StringType(), required=True),
    NestedField(3, "run_id", StringType(), required=True),
    NestedField(4, "file_sha256", StringType(), required=True),
)
_QUARANTINE_ARROW = pa.schema(
    [pa.field(f.name, pa.large_string(), nullable=False) for f in _QUARANTINE_FIELDS]
)

_LINEAGE_DDL = """
CREATE TABLE IF NOT EXISTS runs (
    run_id       text PRIMARY KEY,
    file_sha256  char(64) NOT NULL,
    table_name   text NOT NULL,
    started_at   timestamptz NOT NULL,
    finished_at  timestamptz NOT NULL,
    status       text NOT NULL,
    cost_usd     double precision NOT NULL CHECK (cost_usd >= 0)
);
CREATE TABLE IF NOT EXISTS plan_nodes (
    run_id       text NOT NULL REFERENCES runs (run_id),
    node_id      text NOT NULL,
    column_name  text NOT NULL,
    tier         text NOT NULL,
    attempts     integer NOT NULL CHECK (attempts >= 0),
    accepted     boolean NOT NULL,
    PRIMARY KEY (run_id, node_id, column_name)
);
CREATE TABLE IF NOT EXISTS transforms (
    run_id       text NOT NULL REFERENCES runs (run_id),
    node_id      text NOT NULL,
    attempt      integer NOT NULL CHECK (attempt >= 1),
    code_sha256  char(64) NOT NULL,
    code         text NOT NULL,
    model        text NOT NULL,
    passed       boolean NOT NULL,
    PRIMARY KEY (run_id, node_id, attempt)
);
CREATE TABLE IF NOT EXISTS loads (
    run_id            text NOT NULL REFERENCES runs (run_id),
    file_sha256       char(64) NOT NULL,
    contract_version  text NOT NULL,
    iceberg_table     text NOT NULL,
    snapshot_id       text,
    rows_loaded       integer NOT NULL CHECK (rows_loaded >= 0),
    rows_quarantined  integer NOT NULL CHECK (rows_quarantined >= 0),
    loaded_at         timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT loads_table_idempotency_key UNIQUE (iceberg_table, file_sha256, contract_version)
);
-- Migrate a pre-L1 table keyed by (file_sha256, contract_version) only. Guarded so the usual
-- path takes no ALTER TABLE lock.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'loads_idempotency_key') THEN
        ALTER TABLE loads DROP CONSTRAINT loads_idempotency_key;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'loads_table_idempotency_key') THEN
        ALTER TABLE loads ADD CONSTRAINT loads_table_idempotency_key
            UNIQUE (iceberg_table, file_sha256, contract_version);
    END IF;
END $$;
CREATE TABLE IF NOT EXISTS flags (
    run_id       text NOT NULL REFERENCES runs (run_id),
    pk           text NOT NULL,
    column_name  text NOT NULL,
    PRIMARY KEY (run_id, pk, column_name)
);
"""


class LoadError(Exception):
    """The output cannot be loaded (bad frame, bad ids, schema drift, unknown snapshot)."""


class LoadResult(BaseModel):
    """Outcome of :func:`load_output`.

    Attributes:
        snapshot_id: The main table's snapshot holding this file's rows (``None`` when the
            output had no rows, e.g. a quarantined file).
        rows_loaded: Rows in the main table from this file.
        rows_quarantined: Rows written to the quarantine table from this file.
        skipped: True when the idempotency key was already recorded; nothing was written.
        recovered: True when an earlier attempt had committed to Iceberg but not recorded the
            load; that data was reused instead of appended again.
    """

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    snapshot_id: str | None
    rows_loaded: int = Field(ge=0)
    rows_quarantined: int = Field(ge=0)
    skipped: bool
    recovered: bool = False


class PlanNodeRecord(BaseModel):
    """One ``plan_nodes`` lineage row, read from ``SystemOutput.meta["plan_nodes"]``."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    node_id: OpaqueId
    column_name: Annotated[str, Field(min_length=1, max_length=256)]
    tier: ModelTier
    attempts: Annotated[int, Field(ge=0)]
    accepted: bool


class TransformRecord(BaseModel):
    """One ``transforms`` lineage row, read from ``SystemOutput.meta["transforms"]``."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    node_id: OpaqueId
    attempt: Annotated[int, Field(ge=1)]
    code: Annotated[str, Field(max_length=MAX_CODE_CHARS)]
    model: Annotated[str, Field(min_length=1, max_length=256)]
    passed: bool


@dataclass(frozen=True, slots=True)
class LoadPlan:
    """What a load must still write. ``recovering`` means some table was already committed."""

    skip: bool
    append_main: bool
    append_quarantine: bool
    recovering: bool = False


# --------------------------------------------------------------------------------------------
# Pure helpers (unit-tested without services)
# --------------------------------------------------------------------------------------------


def iceberg_type(dtype: Dtype) -> IcebergType:
    """The Iceberg type for a contract dtype."""
    try:
        return _ICEBERG_TYPES[dtype]
    except KeyError:
        raise LoadError(f"no Iceberg type for contract dtype {dtype!r}") from None


def iceberg_schema(contract: Contract) -> Schema:
    """The main-table schema: contract order, field ids from 1, key columns required."""
    keys = set(contract.primary_key)
    fields = [
        NestedField(i, c.name, iceberg_type(c.dtype), required=c.name in keys)
        for i, c in enumerate(contract.columns, start=1)
    ]
    ids = [f.field_id for f in fields if f.name in keys]
    return Schema(*fields, identifier_field_ids=ids)


def quarantine_schema() -> Schema:
    """The ``<table>_quarantine`` schema."""
    return Schema(*_QUARANTINE_FIELDS)


def arrow_schema(contract: Contract) -> pa.Schema:
    """The Arrow schema written to the main table (key columns non-nullable)."""
    keys = set(contract.primary_key)
    return pa.schema(
        [pa.field(c.name, _ARROW_TYPES[c.dtype], nullable=c.name not in keys)
         for c in contract.columns]
    )  # fmt: skip


def to_arrow(df: pl.DataFrame, contract: Contract) -> pa.Table:
    """Convert a contract-shaped frame to Arrow exactly.

    Raises:
        LoadError: If the frame's schema is not exactly the contract's Polars schema (names,
            order, dtypes), or a key is null or duplicated.
    """
    expected = list(contract.polars_schema().items())
    actual = list(df.schema.items())
    if actual != expected:
        bad = sorted(
            {n for n, _ in actual if (n, df.schema[n]) not in expected}
            | {n for n, _ in expected if (n, df.schema.get(n)) not in actual}
            | {a[0] for a, e in zip(actual, expected, strict=False) if a[0] != e[0]}
        )
        raise LoadError(
            f"frame schema does not match contract {contract.name}.{contract.version}: {bad}"
        )
    keys = df.select(contract.primary_key)
    if keys.null_count().sum_horizontal().item() > 0:
        raise LoadError(f"null primary key in {contract.name} output")
    if keys.is_duplicated().any():
        raise LoadError(f"duplicate primary key in {contract.name} output")
    return df.to_arrow().cast(arrow_schema(contract))


def pk_text(pk: PK) -> str:
    """A key tuple as compact JSON text (``(7, 3)`` -> ``[7,3]``); stable and unambiguous."""
    return json.dumps(list(pk), separators=(",", ":"), ensure_ascii=False, default=str)


def load_status(out: SystemOutput, status: str | None) -> str:
    """The ``runs.status`` of a load: the pipeline's ``status``, else derived from ``out``.

    Derived: ``quarantined`` when the whole file or any row was held back, else ``succeeded``
    (contract "Statuses").

    Raises:
        LoadError: ``status`` is not one an output may be loaded with (e.g. ``failed``).
    """
    if status is None:
        return STATUS_QUARANTINED if out.file_quarantined or out.quarantined else STATUS_SUCCEEDED
    if status not in LOADABLE_STATUSES:
        raise LoadError(f"refusing to load an output with run status {status!r}")
    return status


def plan_load(
    *,
    recorded: bool,
    main_committed: bool,
    quarantine_committed: bool,
    n_rows: int,
    n_quarantined: int,
) -> LoadPlan:
    """Decide what a load must write, given what is already recorded or committed."""
    if recorded:
        return LoadPlan(skip=True, append_main=False, append_quarantine=False)
    return LoadPlan(
        skip=False,
        append_main=n_rows > 0 and not main_committed,
        append_quarantine=n_quarantined > 0 and not quarantine_committed,
        recovering=main_committed or quarantine_committed,
    )


def committed_snapshot_id(
    snapshots: Iterable[tuple[int, Mapping[str, Any]]], file_sha256: str, contract_version: str
) -> int | None:
    """The first snapshot whose summary carries this idempotency key, or ``None``."""
    for snapshot_id, summary in snapshots:
        if (
            summary.get(PROP_FILE_SHA256) == file_sha256
            and summary.get(PROP_CONTRACT_VERSION) == contract_version
        ):
            return snapshot_id
    return None


def advisory_key(*parts: str) -> int:
    """A stable signed 64-bit Postgres advisory-lock key for ``parts``."""
    digest = hashlib.sha256("\0".join(parts).encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


# --------------------------------------------------------------------------------------------
# Service-facing code
# --------------------------------------------------------------------------------------------


def _catalog_properties(settings: Settings) -> dict[str, str]:
    # Holds secrets: never log or print the returned dict.
    uri = settings.postgres_dsn.replace("postgresql://", "postgresql+psycopg://", 1)
    return {
        "uri": uri,
        "warehouse": f"s3://{settings.warehouse_bucket}/",
        "py-io-impl": "pyiceberg.io.pyarrow.PyArrowFileIO",
        "s3.endpoint": settings.s3_endpoint,
        "s3.access-key-id": settings.rustfs_access_key.get_secret_value(),
        "s3.secret-access-key": settings.rustfs_secret_key.get_secret_value(),
        "s3.region": S3_REGION,
        "s3.connect-timeout": str(S3_TIMEOUT_S),
        "s3.request-timeout": str(S3_TIMEOUT_S),
    }


@contextmanager
def catalog(settings: Settings) -> Iterator[SqlCatalog]:
    """The Iceberg SQL catalog on Postgres; its connection pool is disposed on exit."""
    cat = SqlCatalog(CATALOG_NAME, **_catalog_properties(settings))
    try:
        yield cat
    finally:
        cat.engine.dispose()


def _connect(settings: Settings) -> psycopg.Connection:
    return psycopg.connect(settings.postgres_dsn, connect_timeout=PG_CONNECT_TIMEOUT_S)


def set_lock_timeouts(conn: psycopg.Connection) -> None:
    """Bound the current transaction's lock waits (transaction-local ``set_config``)."""
    conn.execute(_SET_CONFIG, ("lock_timeout", LOCK_TIMEOUT))
    conn.execute(_SET_CONFIG, ("statement_timeout", LOCK_WAIT_STATEMENT_TIMEOUT))


def restore_statement_timeout(conn: psycopg.Connection) -> None:
    """After the locks are held: the statement timeout for the rest of the transaction."""
    conn.execute(_SET_CONFIG, ("statement_timeout", STATEMENT_TIMEOUT))


def ensure_lineage_tables(conn: psycopg.Connection) -> None:
    """Create the lineage tables if missing (serialised, so concurrent first loads are safe)."""
    with conn.transaction():
        set_lock_timeouts(conn)
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (advisory_key("sluice.lineage.ddl"),))
        restore_statement_timeout(conn)
        conn.execute(_LINEAGE_DDL)


def ensure_namespace(cat: SqlCatalog, namespace: str) -> None:
    """Create ``namespace`` if missing, tolerating a concurrent creator.

    The SQL catalog checks then inserts, so two first loads of different tables can both pass
    the check; the loser gets an ``IntegrityError``, which is fine if the namespace now exists.
    """
    try:
        cat.create_namespace_if_not_exists(namespace)
    except IntegrityError:
        if not cat.namespace_exists(namespace):
            raise


def _check_ident(name: str, what: str) -> str:
    if not _IDENT.fullmatch(name):
        raise LoadError(f"invalid {what} {name!r}")
    return name


def _fields(schema: Schema) -> list[tuple[str, IcebergType, bool]]:
    return [(f.name, f.field_type, f.required) for f in schema.fields]


def _ensure_table(cat: SqlCatalog, identifier: str, schema: Schema) -> Table:
    table = cat.create_table_if_not_exists(identifier, schema=schema)
    if _fields(table.schema()) != _fields(schema):
        raise LoadError(f"Iceberg table {identifier} exists with a different schema")
    return table


def _ancestry(table: Table) -> Iterator[tuple[int, Mapping[str, Any]]]:
    for snap in ancestors_of(table.current_snapshot(), table.metadata):
        yield snap.snapshot_id, (snap.summary or {})


def _quarantine_arrow(
    out: SystemOutput, contract: Contract, run_id: str, file_sha256: str
) -> pa.Table:
    reasons = out.meta.get("quarantine_reasons") or {}
    if not isinstance(reasons, Mapping) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in reasons.items()
    ):
        raise LoadError('meta["quarantine_reasons"] must map pk text to a reason string')
    default = DEFAULT_FK_REASON if contract.foreign_keys else DEFAULT_REASON
    pks = sorted(pk_text(pk) for pk in out.quarantined)
    return pa.table(
        {
            "pk": pks,
            "reason": [reasons.get(pk, default) for pk in pks],
            "run_id": [run_id] * len(pks),
            "file_sha256": [file_sha256] * len(pks),
        },
        schema=_QUARANTINE_ARROW,
    )


def _lineage_records(
    meta: Mapping[str, Any],
) -> tuple[list[PlanNodeRecord], list[TransformRecord]]:
    try:
        nodes = [PlanNodeRecord.model_validate(n) for n in meta.get("plan_nodes") or []]
        transforms = [TransformRecord.model_validate(t) for t in meta.get("transforms") or []]
    except (ValidationError, TypeError) as exc:
        raise LoadError(f"invalid lineage in output meta: {exc}") from exc
    return nodes, transforms


def _validate_ids(run_id: str, file_sha256: str) -> None:
    if not _RUN_ID.fullmatch(run_id):
        raise LoadError("run_id must match [A-Za-z0-9_-]{1,64}")
    if not _SHA256.fullmatch(file_sha256):
        raise LoadError("file_sha256 must be 64 lowercase hex characters")


@dataclass(frozen=True, slots=True)
class _Record:
    run_id: str
    file_sha256: str
    contract: Contract
    iceberg_table: str
    snapshot_id: str | None
    status: str
    started_at: datetime
    finished_at: datetime
    cost_usd: float
    rows_loaded: int
    rows_quarantined: int
    flags: Sequence[tuple[str, str]]
    nodes: Sequence[PlanNodeRecord]
    transforms: Sequence[TransformRecord]


def _record_run(conn: psycopg.Connection, rec: _Record) -> None:
    """Insert ``runs`` and the run's ``flags``/``plan_nodes``/``transforms`` (caller's txn)."""
    conn.execute(
        """
        INSERT INTO runs (run_id, file_sha256, table_name, started_at, finished_at, status,
                          cost_usd)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (run_id) DO UPDATE
           SET finished_at = EXCLUDED.finished_at, status = EXCLUDED.status,
               cost_usd = EXCLUDED.cost_usd
        """,
        (
            rec.run_id,
            rec.file_sha256,
            rec.contract.name,
            rec.started_at,
            rec.finished_at,
            rec.status,
            rec.cost_usd,
        ),
    )
    with conn.cursor() as cur:
        if rec.flags:
            cur.executemany(
                "INSERT INTO flags (run_id, pk, column_name) VALUES (%s, %s, %s) "
                "ON CONFLICT DO NOTHING",
                [(rec.run_id, pk, col) for pk, col in rec.flags],
            )
        if rec.nodes:
            cur.executemany(
                "INSERT INTO plan_nodes (run_id, node_id, column_name, tier, attempts, accepted) "
                "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
                [
                    (rec.run_id, n.node_id, n.column_name, n.tier, n.attempts, n.accepted)
                    for n in rec.nodes
                ],
            )
        if rec.transforms:
            cur.executemany(
                "INSERT INTO transforms (run_id, node_id, attempt, code_sha256, code, model, "
                "passed) VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
                [
                    (
                        rec.run_id,
                        t.node_id,
                        t.attempt,
                        hashlib.sha256(t.code.encode("utf-8")).hexdigest(),
                        t.code,
                        t.model,
                        t.passed,
                    )
                    for t in rec.transforms
                ],
            )


def _record_load(conn: psycopg.Connection, rec: _Record) -> None:
    """Record one load (inside the caller's transaction, under the key's advisory lock).

    The ``loads`` row (the idempotency key) is written only when the main table holds a
    snapshot for this file; a legacy key row without a snapshot is deleted first.
    """
    _record_run(conn, rec)
    key = (rec.iceberg_table, rec.file_sha256, rec.contract.version)
    conn.execute(_DELETE_NULL_LOAD, key)
    if rec.snapshot_id is not None:
        conn.execute(
            """
            INSERT INTO loads (run_id, file_sha256, contract_version, iceberg_table, snapshot_id,
                               rows_loaded, rows_quarantined)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (
                rec.run_id,
                rec.file_sha256,
                rec.contract.version,
                rec.iceberg_table,
                rec.snapshot_id,
                rec.rows_loaded,
                rec.rows_quarantined,
            ),
        )


_SELECT_LOAD = (
    "SELECT snapshot_id, rows_loaded, rows_quarantined FROM loads "
    "WHERE iceberg_table = %s AND file_sha256 = %s AND contract_version = %s "
    "AND snapshot_id IS NOT NULL"
)
# A legacy row (written before Ruling F1) that claimed the key without a main-table snapshot.
_DELETE_NULL_LOAD = (
    "DELETE FROM loads WHERE iceberg_table = %s AND file_sha256 = %s AND contract_version = %s "
    "AND snapshot_id IS NULL"
)


def _recorded(
    conn: psycopg.Connection, iceberg_table: str, file_sha256: str, contract_version: str
) -> LoadResult | None:
    row = conn.execute(_SELECT_LOAD, (iceberg_table, file_sha256, contract_version)).fetchone()
    if row is None:
        return None
    return LoadResult(snapshot_id=row[0], rows_loaded=row[1], rows_quarantined=row[2], skipped=True)


def find_load(
    contract: Contract, file_sha256: str, *, settings: Settings, namespace: str = NAMESPACE
) -> LoadResult | None:
    """The recorded load of this file into ``<namespace>.<contract.name>``, or ``None``.

    A lock-free read (``skipped=True`` when found) for callers that want to avoid re-running the
    pipeline; :func:`load_output` re-checks under its locks. Never creates anything.
    """
    if not _SHA256.fullmatch(file_sha256):
        raise LoadError("file_sha256 must be 64 lowercase hex characters")
    ident = f"{_check_ident(namespace, 'namespace')}.{_check_ident(contract.name, 'table name')}"
    with _connect(settings) as conn, conn.transaction():
        conn.execute(_SET_CONFIG, ("statement_timeout", LOCK_WAIT_STATEMENT_TIMEOUT))
        exists = conn.execute("SELECT to_regclass('public.loads') IS NOT NULL").fetchone()
        if not exists or not exists[0]:
            return None
        return _recorded(conn, ident, file_sha256, contract.version)


def load_output(
    out: SystemOutput,
    contract: Contract,
    *,
    run_id: str,
    file_sha256: str,
    settings: Settings,
    namespace: str = NAMESPACE,
    status: str | None = None,
) -> LoadResult:
    """Append ``out`` to ``<namespace>.<table>`` (+ quarantine table) once per file and contract.

    Everything is validated before anything is written. See the module docstring for the
    idempotency protocol. ``status`` is the pipeline's final status, recorded in ``runs``
    (default: derived from ``out``, see :func:`load_status`). A whole-file quarantine
    (``out.file_quarantined``) writes nothing to Iceberg and claims no idempotency key: only its
    ``runs`` row and lineage are recorded, so the file can be run again (Ruling F1).

    Raises:
        LoadError: A status that must not load (``failed``), invalid ids, frame, lineage meta,
            or an existing table with another schema.
    """
    run_status = load_status(out, status)
    _validate_ids(run_id, file_sha256)
    _check_ident(namespace, "namespace")
    name = _check_ident(contract.name, "table name")
    main_arrow = to_arrow(out.df, contract)
    quarantine_arrow = _quarantine_arrow(out, contract, run_id, file_sha256)
    flags = sorted((pk_text(pk), col) for pk, col in out.flags)
    nodes, transforms = _lineage_records(out.meta)
    main_id = f"{namespace}.{name}"
    quarantine_id = f"{main_id}{QUARANTINE_SUFFIX}"
    finished_at = datetime.now(UTC)
    started_at = finished_at - timedelta(seconds=max(out.latency_s, 0.0))
    record = _Record(
        run_id=run_id,
        file_sha256=file_sha256,
        contract=contract,
        iceberg_table=main_id,
        snapshot_id=None,
        status=run_status,
        started_at=started_at,
        finished_at=finished_at,
        cost_usd=out.cost_usd,
        rows_loaded=main_arrow.num_rows,
        rows_quarantined=quarantine_arrow.num_rows,
        flags=flags,
        nodes=nodes,
        transforms=transforms,
    )

    if out.file_quarantined:
        with _connect(settings) as conn:
            ensure_lineage_tables(conn)
            with conn.transaction():
                restore_statement_timeout(conn)
                _record_run(conn, dataclasses.replace(record, rows_loaded=0, rows_quarantined=0))
        return LoadResult(snapshot_id=None, rows_loaded=0, rows_quarantined=0, skipped=False)

    with _connect(settings) as conn:
        ensure_lineage_tables(conn)
        with conn.transaction():
            set_lock_timeouts(conn)
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (advisory_key("table", main_id),))
            conn.execute(
                "SELECT pg_advisory_xact_lock(%s)",
                (advisory_key(main_id, file_sha256, contract.version),),
            )
            restore_statement_timeout(conn)
            prior = _recorded(conn, main_id, file_sha256, contract.version)
            if prior is not None:
                return prior
            with catalog(settings) as cat:
                ensure_namespace(cat, namespace)
                main = _ensure_table(cat, main_id, iceberg_schema(contract))
                quarantine = _ensure_table(cat, quarantine_id, quarantine_schema())
                main_snap = committed_snapshot_id(_ancestry(main), file_sha256, contract.version)
                quarantine_snap = committed_snapshot_id(
                    _ancestry(quarantine), file_sha256, contract.version
                )
                plan = plan_load(
                    recorded=False,
                    main_committed=main_snap is not None,
                    quarantine_committed=quarantine_snap is not None,
                    n_rows=main_arrow.num_rows,
                    n_quarantined=quarantine_arrow.num_rows,
                )
                props = {
                    PROP_FILE_SHA256: file_sha256,
                    PROP_CONTRACT_VERSION: contract.version,
                    PROP_RUN_ID: run_id,
                }
                if plan.append_quarantine:
                    quarantine.append(quarantine_arrow, snapshot_properties=props)
                if plan.append_main:
                    main.append(main_arrow, snapshot_properties=props)
                    main_snap = committed_snapshot_id(
                        _ancestry(main), file_sha256, contract.version
                    )
            snapshot_id = str(main_snap) if main_snap is not None else None
            _record_load(conn, dataclasses.replace(record, snapshot_id=snapshot_id))
    return LoadResult(
        snapshot_id=snapshot_id,
        rows_loaded=main_arrow.num_rows,
        rows_quarantined=quarantine_arrow.num_rows,
        skipped=False,
        recovered=plan.recovering,
    )


def rollback(
    table: str, snapshot_id: str, *, settings: Settings, namespace: str = NAMESPACE
) -> list[str]:
    """Make ``snapshot_id`` the current snapshot of ``<namespace>.<table>``.

    ``snapshot_id`` must be the current snapshot or one of its ancestors. Afterwards, the
    ``loads`` rows of this table whose snapshot is not an ancestor of the new current snapshot
    are deleted (those files can then be loaded again) and their ``runs`` are marked
    ``rolled_back``. The rolled-back snapshots stay in the table metadata until expired. Only
    the named table is rolled back: the quarantine table is untouched, and a reload does not
    duplicate its rows because those appends are still in its ancestry. Safe to retry: when
    ``snapshot_id`` is already current, the Iceberg step is skipped and the cleanup runs again.

    Returns:
        The run ids whose loads were rolled back.

    Raises:
        LoadError: Bad identifiers, unknown table, or a snapshot that is not an ancestor.
    """
    ident = f"{_check_ident(namespace, 'namespace')}.{_check_ident(table, 'table name')}"
    try:
        target = int(snapshot_id)
    except ValueError:
        raise LoadError(f"snapshot id must be an integer, got {snapshot_id!r}") from None
    with _connect(settings) as conn:
        ensure_lineage_tables(conn)
        with conn.transaction():
            set_lock_timeouts(conn)
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (advisory_key("table", ident),))
            restore_statement_timeout(conn)
            with catalog(settings) as cat:
                try:
                    tbl = cat.load_table(ident)
                except NoSuchTableError:
                    raise LoadError(f"no Iceberg table {ident}") from None
                if tbl.metadata.snapshot_by_id(target) is None:
                    raise LoadError(f"unknown snapshot {target} for {ident}")
                current = tbl.current_snapshot()
                if current is None or current.snapshot_id != target:
                    try:
                        tbl.manage_snapshots().rollback_to_snapshot(target).commit()
                    except ValueError as exc:
                        raise LoadError(f"cannot roll back {ident}: {exc}") from exc
                kept = [str(sid) for sid, _ in _ancestry(tbl)]
            rows = conn.execute(
                "DELETE FROM loads WHERE iceberg_table = %s AND snapshot_id IS NOT NULL "
                "AND NOT (snapshot_id = ANY(%s)) RETURNING run_id",
                (ident, kept),
            ).fetchall()
            run_ids = [r[0] for r in rows]
            if run_ids:
                conn.execute(
                    "UPDATE runs SET status = %s WHERE run_id = ANY(%s)",
                    (STATUS_ROLLED_BACK, run_ids),
                )
    return run_ids
