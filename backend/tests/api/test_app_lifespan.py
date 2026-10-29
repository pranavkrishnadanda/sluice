"""App lifespan: the hub is always created; with the worker enabled, interrupted runs are
recovered first, then queued runs are executed, and the worker stops on shutdown.

``TestClient`` runs the lifespan in-process; nothing binds a port.
"""

import logging
import time
from pathlib import Path

import pytest
from api_fakes import FakeStore, happy, scripted
from fastapi.testclient import TestClient

from sluice.api.app import create_app
from sluice.api.blobs import LocalBlobStore
from sluice.api.hub import EventHub
from sluice.api.settings import ApiSettings
from sluice.api.store import TERMINAL_STATUSES
from sluice.api.worker import RESTART_SUMMARY, Worker


def _wait_terminal(store: FakeStore, run_id: str, timeout_s: float = 5.0) -> None:
    deadline = time.monotonic() + timeout_s
    while True:
        row = store.get_run(run_id)
        if row is not None and row.status in TERMINAL_STATUSES:
            return
        assert time.monotonic() < deadline, "run did not finish in time"
        time.sleep(0.01)


def test_lifespan_without_worker_creates_the_hub_only(tmp_path: Path) -> None:
    store = FakeStore()
    store.add("r1", status="running")
    blobs = LocalBlobStore(tmp_path)
    app = create_app(settings=ApiSettings(), store=store, blobs=blobs, worker=False)  # type: ignore[arg-type]
    with TestClient(app):
        assert isinstance(app.state.hub, EventHub)
        assert app.state.worker is None
    assert store.finished == []  # no recovery without the worker


def test_lifespan_recovers_interrupted_runs_then_runs_queued_ones(tmp_path: Path) -> None:
    store = FakeStore()
    store.add("old", status="running")
    store.add("new")
    blobs = LocalBlobStore(tmp_path)
    blobs.put_bytes("uploads/new/input.csv", b"o_orderkey\n1\n")
    app = create_app(
        settings=ApiSettings(),
        store=store,  # type: ignore[arg-type]
        blobs=blobs,
        execute=scripted(happy("new")),
    )
    with TestClient(app):
        assert isinstance(app.state.worker, Worker)
        _wait_terminal(store, "new")
    (restart,) = store.terminals("old")
    assert restart.summary == RESTART_SUMMARY
    assert [r for (r, _s, _c) in store.finished] == ["old", "new"]
    assert store.types("new") == ["run_started", "rules_applied", "run_finished"]


def test_lifespan_takes_the_worker_lock_and_releases_it_on_shutdown(tmp_path: Path) -> None:
    store = FakeStore()
    app = create_app(settings=ApiSettings(), store=store, blobs=LocalBlobStore(tmp_path))  # type: ignore[arg-type]
    with TestClient(app):
        (lock,) = store.worker_locks
        assert isinstance(app.state.worker, Worker)
        assert lock.released is False
    assert lock.released is True


def test_lifespan_with_the_worker_lock_taken_neither_recovers_nor_starts_a_worker(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A second API process must not close the first one's running runs (Minor 7)."""
    store = FakeStore()
    store.worker_lock_taken = True
    store.add("theirs", status="running")
    store.add("queued")
    app = create_app(
        settings=ApiSettings(),
        store=store,  # type: ignore[arg-type]
        blobs=LocalBlobStore(tmp_path),
        execute=scripted(happy("queued")),
    )
    with caplog.at_level(logging.ERROR, logger="sluice.api.app"), TestClient(app) as client:
        assert app.state.worker is None
        assert client.get("/healthz").status_code == 200  # still serves requests
        time.sleep(0.1)
    assert store.finished == []  # "theirs" not recovered, "queued" not run here
    assert store.runs["theirs"].status == "running"
    assert store.runs["queued"].status == "queued"
    assert any("worker lock" in r.getMessage() for r in caplog.records)
