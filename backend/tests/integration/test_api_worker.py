"""The job worker end to end on the Compose stack (`make test-integration`), Review Focus 4.

Two uploads of the same file to ``orders``, run two at a time: exactly one loads, the other
ends ``succeeded`` with "file already loaded", and the Iceberg table holds one file's rows.

Real RunStore (Postgres, own schema), S3BlobStore (RustFS, own bucket), Iceberg (own
namespace, so the idempotency key is private to this test) and ``execute_run``; the system is
the rules-only baseline, so no model is called.
"""

import asyncio
import functools
import secrets
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import quote

import polars as pl
import psycopg
import pytest
from pyarrow.fs import S3FileSystem

from sluice.agent import events as ev
from sluice.api.blobs import S3BlobStore
from sluice.api.hub import EventHub
from sluice.api.store import TERMINAL_STATUSES, RunStore
from sluice.api.worker import Worker, preview_key
from sluice.load import iceberg
from sluice.load.iceberg import find_load, load_output
from sluice.runs.execute import RunOutcome, execute_run, file_sha256
from sluice.settings import Settings, get_settings
from sluice.systems.rules_only import RulesOnlySystem

pytestmark = pytest.mark.integration

ROWS = 30


@pytest.fixture
def settings() -> Settings:
    return get_settings()


@pytest.fixture
def store(settings: Settings) -> Iterator[RunStore]:
    schema = f"t_wrk_{secrets.token_hex(4)}"
    with psycopg.connect(settings.postgres_dsn, connect_timeout=5, autocommit=True) as conn:
        conn.execute(f'CREATE SCHEMA "{schema}"')  # generated identifier, not user input
    options = quote(f"-csearch_path={schema}", safe="")
    run_store = RunStore(f"{settings.postgres_dsn}?options={options}")
    run_store.ensure_schema()
    try:
        yield run_store
    finally:
        with psycopg.connect(settings.postgres_dsn, connect_timeout=5, autocommit=True) as conn:
            conn.execute(f'DROP SCHEMA "{schema}" CASCADE')


@pytest.fixture
def blobs(settings: Settings) -> Iterator[S3BlobStore]:
    bucket = f"sluice-test-{secrets.token_hex(4)}"
    blob_store = S3BlobStore.from_settings(settings, bucket)
    try:
        yield blob_store
    finally:
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


@pytest.fixture
def ns() -> str:
    return f"t_{secrets.token_hex(4)}"


def _rules_execute(ns: str) -> Any:
    def execute(path: Path, table: str, *, run_id: str, emit: Any, **kw: Any) -> RunOutcome:
        return execute_run(
            path,
            table,
            run_id=run_id,
            emit=emit,
            run_fn=RulesOnlySystem().run,
            load_fn=functools.partial(load_output, namespace=ns),
            loaded_fn=functools.partial(find_load, namespace=ns),
            reference_fn=lambda _t: {},
            **kw,
        )

    return execute


def test_worker_two_uploads_of_one_file_load_it_once(
    store: RunStore,
    blobs: S3BlobStore,
    ns: str,
    settings: Settings,
    orders_df: pl.DataFrame,
    tmp_path: Path,
) -> None:
    csv = tmp_path / "orders.csv"
    orders_df.head(ROWS).write_csv(csv)
    body, sha = csv.read_bytes(), file_sha256(csv)
    for run_id in ("up-a", "up-b"):
        key = f"uploads/{run_id}/input.csv"
        blobs.put_bytes(key, body)
        store.create_run(
            run_id=run_id, file_name="orders.csv", table="orders", blob_key=key, file_sha256=sha
        )

    async def go() -> None:
        worker = Worker(store, blobs, EventHub(), concurrency=2, execute=_rules_execute(ns))
        await worker.recover()
        await worker.start()
        try:
            deadline = time.monotonic() + 120
            while True:
                rows = [store.get_run(r) for r in ("up-a", "up-b")]
                if all(r is not None and r.status in TERMINAL_STATUSES for r in rows):
                    return
                assert time.monotonic() < deadline, "runs did not finish in time"
                await asyncio.sleep(0.1)
        finally:
            await worker.stop()

    asyncio.run(go())

    events = {r: store.events_after(r, 0) for r in ("up-a", "up-b")}
    loaders = [r for r, evs in events.items() if any(isinstance(e, ev.Loaded) for e in evs)]
    assert len(loaders) == 1
    (loader,) = loaders
    (other,) = {"up-a", "up-b"} - {loader}

    for run_id, evs in events.items():
        finals = [e for e in evs if isinstance(e, ev.RunFinished)]
        assert len(finals) == 1 and evs[-1] is finals[0]
        assert [e.seq for e in evs] == list(range(1, len(evs) + 1))
        row = store.get_run(run_id)
        assert row is not None and row.status == "succeeded" and row.rows == ROWS
        assert isinstance(evs[0], ev.RunStarted) and evs[0].file_name == "orders.csv"
    other_final = events[other][-1]
    assert isinstance(other_final, ev.RunFinished)
    assert "file already loaded" in other_final.summary

    phases = [e.phase for e in events[loader] if isinstance(e, ev.Activity)]
    assert phases == ["loading", "previewing"]  # stored in Postgres and parsed back

    loader_artefacts = store.get_artefacts(loader)
    assert loader_artefacts is not None and loader_artefacts.preview_key == preview_key(loader)
    assert pl.read_parquet(blobs.get_bytes(preview_key(loader))).height == ROWS
    other_artefacts = store.get_artefacts(other)
    assert other_artefacts is not None and other_artefacts.preview_key is None

    with iceberg.catalog(settings) as cat:
        table = cat.load_table(f"{ns}.orders")
        assert table.scan().to_arrow().num_rows == ROWS
