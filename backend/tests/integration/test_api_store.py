"""RunStore on the Compose Postgres and S3BlobStore on the Compose RustFS (`make test-integration`).

Isolation: every test gets its own Postgres schema (``search_path`` set through the DSN's
``options``), so ``claim_next`` can never claim a real queued run, and its own RustFS bucket.
Both are dropped afterwards.
"""

import asyncio
import secrets
import threading
from collections.abc import AsyncIterator, Iterator
from urllib.parse import quote

import psycopg
import pytest
from pyarrow.fs import FileType, S3FileSystem

from sluice.agent import events as ev
from sluice.api.blobs import S3BlobStore, TooLarge
from sluice.api.store import DuplicateEvent, RunNotActive, RunNotFound, RunRow, RunStore
from sluice.settings import Settings, get_settings

pytestmark = pytest.mark.integration

SHA = "b" * 64
AT = "2026-10-27T10:14:03.000Z"


@pytest.fixture
def settings() -> Settings:
    return get_settings()


@pytest.fixture
def store(settings: Settings) -> Iterator[RunStore]:
    schema = f"t_api_{secrets.token_hex(4)}"
    with psycopg.connect(settings.postgres_dsn, connect_timeout=5, autocommit=True) as conn:
        conn.execute(f'CREATE SCHEMA "{schema}"')  # generated identifier, not user input
    options = quote(f"-csearch_path={schema}", safe="")
    run_store = RunStore(f"{settings.postgres_dsn}?options={options}")
    run_store.ensure_schema()
    run_store.ensure_schema()  # idempotent
    try:
        yield run_store
    finally:
        with psycopg.connect(settings.postgres_dsn, connect_timeout=5, autocommit=True) as conn:
            conn.execute(f'DROP SCHEMA "{schema}" CASCADE')


def _create(store: RunStore, run_id: str) -> None:
    store.create_run(
        run_id=run_id,
        file_name=f"{run_id}.csv",
        table="orders",
        blob_key=f"uploads/{run_id}/input.csv",
        file_sha256=SHA,
    )


def _stamp(event: ev.EventModel, seq: int) -> ev.EventModel:
    return event.model_copy(update={"seq": seq, "at": AT})


# --------------------------------------------------------------------------------------------
# RunStore
# --------------------------------------------------------------------------------------------


def test_create_run_returns_queued_summary(store: RunStore) -> None:
    summary = store.create_run(
        run_id="r1",
        file_name="orders.csv",
        table="orders",
        blob_key="uploads/r1/input.csv",
        file_sha256=SHA,
    )
    assert (summary.id, summary.status, summary.rows, summary.cost_usd) == ("r1", "queued", None, 0)
    assert summary.created_at.endswith("Z")


def test_list_runs_newest_first_and_limited(store: RunStore) -> None:
    for run_id in ("r1", "r2", "r3"):
        _create(store, run_id)
    assert [r.id for r in store.list_runs()] == ["r3", "r2", "r1"]
    assert [r.id for r in store.list_runs(2)] == ["r3", "r2"]


def test_get_run_returns_row_or_none(store: RunStore) -> None:
    _create(store, "r1")
    row = store.get_run("r1")
    assert isinstance(row, RunRow)
    assert (row.blob_key, row.file_sha256, row.started_at) == ("uploads/r1/input.csv", SHA, None)
    assert row.summary().id == "r1"
    assert store.get_run("missing") is None


def test_claim_next_returns_oldest_queued_once(store: RunStore) -> None:
    _create(store, "r1")
    _create(store, "r2")
    first = store.claim_next()
    second = store.claim_next()
    assert first is not None and second is not None
    assert (first.id, first.status) == ("r1", "running")
    assert first.started_at is not None
    assert second.id == "r2"
    assert store.claim_next() is None


def test_concurrent_claimers_never_get_the_same_row(store: RunStore) -> None:
    n_runs, n_threads = 40, 8
    for i in range(n_runs):
        _create(store, f"r{i:03d}")
    claimed: list[str] = []
    lock = threading.Lock()
    start = threading.Barrier(n_threads)

    def claimer() -> None:
        start.wait(timeout=10)
        while (row := store.claim_next()) is not None:
            with lock:
                claimed.append(row.id)

    threads = [threading.Thread(target=claimer) for _ in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert sorted(claimed) == [f"r{i:03d}" for i in range(n_runs)]  # each exactly once


def test_events_after_returns_later_events_in_order(store: RunStore) -> None:
    _create(store, "r1")
    built: list[ev.EventModel] = [
        ev.RunStarted(run_id="r1", file_name="r1.csv", table="orders", rows=None),
        ev.Profiled(issues=[]),
        ev.RulesApplied(actions=["trim"]),
        ev.PlanCreated(nodes=[]),
        ev.Verified(checks_passed=1, checks_total=1),
        ev.Loaded(rows_loaded=3, rows_quarantined=0, snapshot_id="42"),
        ev.RunFinished(status="succeeded", summary="ok", cost_usd=0.5, tokens=7),
    ]
    stamped = [_stamp(e, i) for i, e in enumerate(built, start=1)]
    for event in reversed(stamped):  # insertion order must not matter
        store.append_event("r1", event)
    assert store.events_after("r1", 3) == stamped[3:]
    assert store.events_after("r1", 0) == stamped
    assert store.events_after("r1", 7) == []


def test_append_event_duplicate_seq_raises(store: RunStore) -> None:
    _create(store, "r1")
    store.append_event("r1", _stamp(ev.Profiled(issues=[]), 1))
    with pytest.raises(DuplicateEvent):
        store.append_event("r1", _stamp(ev.RulesApplied(actions=[]), 1))


def test_append_event_unknown_run_raises(store: RunStore) -> None:
    with pytest.raises(RunNotFound):
        store.append_event("nope", _stamp(ev.Profiled(issues=[]), 1))


def test_update_progress_and_finish(store: RunStore) -> None:
    _create(store, "r1")
    store.claim_next()
    store.update_progress("r1", rows=120)
    store.update_progress("r1", cost_usd=0.25)
    row = store.get_run("r1")
    assert row is not None and (row.rows, row.cost_usd) == (120, 0.25)
    store.finish("r1", "quarantined", 0.5)
    row = store.get_run("r1")
    assert row is not None
    assert (row.status, row.cost_usd) == ("quarantined", 0.5)
    assert row.finished_at is not None


def test_finish_twice_raises_and_keeps_first_status(store: RunStore) -> None:
    _create(store, "r1")
    store.finish("r1", "failed", 0.0)
    with pytest.raises(RunNotActive):
        store.finish("r1", "succeeded", 1.0)
    row = store.get_run("r1")
    assert row is not None and row.status == "failed"


def test_finish_unknown_run_raises(store: RunStore) -> None:
    with pytest.raises(RunNotFound):
        store.finish("nope", "failed", 0.0)


def test_artefacts_upsert_and_get(store: RunStore) -> None:
    _create(store, "r1")
    assert store.get_artefacts("r1") is None
    store.save_artefacts("r1", preview_key=None, transforms=[])
    store.save_artefacts(
        "r1", preview_key="runs/r1/preview.parquet", transforms=[{"id": "t1", "code": "x"}]
    )
    art = store.get_artefacts("r1")
    assert art is not None
    assert art.preview_key == "runs/r1/preview.parquet"
    assert art.transforms == [{"id": "t1", "code": "x"}]


def _record(transform_id: str, attempt: int, passed: bool) -> dict[str, object]:
    return {
        "transform_id": transform_id,
        "node_id": "n1",
        "attempt": attempt,
        "code": "def transform(s): return s",
        "model": "small",
        "passed": passed,
    }


def test_append_transform_creates_the_row_and_appends_once_per_id(store: RunStore) -> None:
    _create(store, "r1")
    store.append_transform("r1", _record("t1", 1, False))
    art = store.get_artefacts("r1")
    assert art is not None
    assert art.preview_key is None
    assert art.transforms == [_record("t1", 1, False)]
    assert art.saved is False  # live transforms only: the end-of-run save has not happened

    store.append_transform("r1", _record("t2", 2, True))
    store.append_transform("r1", _record("t1", 1, False))  # a repeat is ignored
    art = store.get_artefacts("r1")
    assert art is not None
    assert [t["transform_id"] for t in art.transforms] == ["t1", "t2"]


def test_append_transform_concurrent_appends_lose_nothing(store: RunStore) -> None:
    _create(store, "r1")
    ids = [f"t{i}" for i in range(8)]
    threads = [
        threading.Thread(target=store.append_transform, args=("r1", _record(t, 1, True)))
        for t in ids
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    art = store.get_artefacts("r1")
    assert art is not None
    assert sorted(t["transform_id"] for t in art.transforms) == ids


def test_append_transform_unknown_run_or_bad_record_raises(store: RunStore) -> None:
    with pytest.raises(RunNotFound):
        store.append_transform("nope", _record("t1", 1, True))
    _create(store, "r1")
    with pytest.raises(ValueError, match="transform_id"):
        store.append_transform("r1", {"code": "x"})


def test_save_artefacts_reconciles_live_transforms_by_id(store: RunStore) -> None:
    _create(store, "r1")
    store.append_transform("r1", _record("t1", 1, False))
    store.append_transform("r1", _record("t9", 1, False))
    store.save_artefacts(
        "r1",
        preview_key="runs/r1/preview.parquet",
        transforms=[_record("t1", 1, True), _record("t2", 2, True)],
    )
    store.save_artefacts(  # idempotent: a repeat changes nothing
        "r1",
        preview_key="runs/r1/preview.parquet",
        transforms=[_record("t1", 1, True), _record("t2", 2, True)],
    )
    art = store.get_artefacts("r1")
    assert art is not None
    assert art.preview_key == "runs/r1/preview.parquet"
    assert art.saved is True
    assert [(t["transform_id"], t["passed"]) for t in art.transforms] == [
        ("t1", True),
        ("t2", True),
        ("t9", False),
    ]


def test_save_artefacts_without_preview_or_transforms_settles_and_keeps_live_ones(
    store: RunStore,
) -> None:
    # The worker's settle step for a failed, cancelled or recovered run.
    _create(store, "r1")
    store.append_transform("r1", _record("t1", 1, False))
    store.save_artefacts("r1", preview_key=None, transforms=[])
    art = store.get_artefacts("r1")
    assert art is not None
    assert (art.preview_key, art.saved) == (None, True)
    assert [t["transform_id"] for t in art.transforms] == ["t1"]
    _create(store, "r2")  # no live transforms: the row is created, settled and empty
    store.save_artefacts("r2", preview_key=None, transforms=[])
    art = store.get_artefacts("r2")
    assert art is not None and art.saved and art.transforms == []


def test_ensure_schema_marks_artefacts_saved_before_the_column_existed(store: RunStore) -> None:
    _create(store, "r1")
    with psycopg.connect(store._dsn, connect_timeout=5, autocommit=True) as conn:
        conn.execute("ALTER TABLE run_artefacts DROP COLUMN saved")
        conn.execute("INSERT INTO run_artefacts (run_id, preview_key) VALUES ('r1', NULL)")
    store.ensure_schema()
    store.append_transform("r1", _record("t1", 1, True))
    art = store.get_artefacts("r1")
    assert art is not None and art.saved is True  # rows from before the column: saved


def test_interrupted_lists_running_runs(store: RunStore) -> None:
    _create(store, "r1")
    _create(store, "r2")
    store.claim_next()
    assert [r.id for r in store.interrupted()] == ["r1"]


# --------------------------------------------------------------------------------------------
# S3BlobStore on RustFS
# --------------------------------------------------------------------------------------------

CHUNK = 64 * 1024


@pytest.fixture
def blobs(settings: Settings) -> Iterator[S3BlobStore]:
    bucket = f"sluice-test-{secrets.token_hex(4)}"
    store = S3BlobStore.from_settings(settings, bucket)  # creates the bucket
    try:
        yield store
    finally:
        # The store's filesystem cannot delete buckets; cleanup uses its own.
        admin = S3FileSystem(
            access_key=settings.rustfs_access_key.get_secret_value(),
            secret_key=settings.rustfs_secret_key.get_secret_value(),
            endpoint_override=settings.s3_endpoint,
            region="us-east-1",
            connect_timeout=5,
            request_timeout=5,
            allow_bucket_deletion=True,
        )
        admin.delete_dir(bucket)


async def _stream(total: int) -> AsyncIterator[bytes]:
    sent = 0
    i = 0
    while sent < total:
        n = min(CHUNK, total - sent)
        yield bytes([i % 251]) * n
        sent += n
        i += 1


def _expected(total: int) -> bytes:
    return b"".join(
        bytes([i % 251]) * min(CHUNK, total - off) for i, off in enumerate(range(0, total, CHUNK))
    )


def test_s3_put_stream_3mb_round_trips(blobs: S3BlobStore) -> None:
    total = 3 * 1024 * 1024
    size = asyncio.run(blobs.put_stream("uploads/r1/input.csv", _stream(total), max_bytes=total))
    assert size == total
    assert blobs.get_bytes("uploads/r1/input.csv") == _expected(total)
    with blobs.open_local("uploads/r1/input.csv") as local:
        assert local.suffix == ".csv"
        assert local.stat().st_size == total
    assert not local.exists()


def test_s3_put_stream_over_limit_leaves_no_object(blobs: S3BlobStore) -> None:
    key = "uploads/r2/input.csv"
    with pytest.raises(TooLarge):
        asyncio.run(blobs.put_stream(key, _stream(3 * 1024 * 1024), max_bytes=1024 * 1024))
    info = blobs.fs.get_file_info(f"{blobs.bucket}/{key}")
    assert info.type == FileType.NotFound


def test_s3_put_get_delete_bytes(blobs: S3BlobStore) -> None:
    blobs.put_bytes("runs/r1/preview.parquet", b"PAR1")
    assert blobs.get_bytes("runs/r1/preview.parquet") == b"PAR1"
    blobs.delete("runs/r1/preview.parquet")
    blobs.delete("runs/r1/preview.parquet")  # missing is fine
    with pytest.raises(FileNotFoundError):
        blobs.get_bytes("runs/r1/preview.parquet")
