"""Job worker: claims queued runs, relays their events (store first, then hub), keeps progress,
guarantees exactly one ``run_finished`` per run, and closes interrupted runs on startup.

No services: an in-memory store, a ``LocalBlobStore`` under ``tmp_path`` and scripted
``execute`` functions (plus the real ``execute_run`` with the rules-only system and a fake load).
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import polars as pl
import pytest
from api_fakes import (
    CREATED,
    ExecuteFn,
    FakeStore,
    attempt_failed,
    finished,
    happy,
    node_finished,
    scripted,
    started,
)

from sluice.agent import events as ev
from sluice.agent.events import EventLog, EventModel
from sluice.api.blobs import LocalBlobStore
from sluice.api.hub import EventHub
from sluice.api.store import TERMINAL_STATUSES, RunRow
from sluice.api.worker import RESTART_SUMMARY, Worker
from sluice.contracts.model import Contract
from sluice.evals.types import SystemOutput
from sluice.load.iceberg import LoadResult
from sluice.runs.execute import RunOutcome, execute_run
from sluice.systems.rules_only import RulesOnlySystem


def _run(make: Callable[[], Awaitable[Any]]) -> Any:
    return asyncio.run(asyncio.wait_for(make(), timeout=10))


async def _until(predicate: Callable[[], bool], timeout_s: float = 5.0) -> None:
    deadline = time.monotonic() + timeout_s
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.01)


def _done(store: FakeStore, *run_ids: str) -> Callable[[], bool]:
    def check() -> bool:
        rows = [store.get_run(r) for r in run_ids]
        return all(r is not None and r.status in TERMINAL_STATUSES for r in rows)

    return check


@pytest.fixture
def blobs(tmp_path: Path) -> LocalBlobStore:
    return LocalBlobStore(tmp_path / "blobs")


def _upload(blobs: LocalBlobStore, run_id: str, body: bytes = b"o_orderkey\n1\n") -> None:
    blobs.put_bytes(f"uploads/{run_id}/input.csv", body)


def _worker(
    store: FakeStore,
    blobs: LocalBlobStore,
    hub: EventHub,
    execute: ExecuteFn,
    *,
    concurrency: int = 1,
    poll_interval_s: float = 0.02,
) -> Worker:
    return Worker(
        store,  # type: ignore[arg-type]
        blobs,
        hub,
        concurrency=concurrency,
        execute=execute,
        settings_fn=lambda: object(),  # type: ignore[arg-type, return-value]
        poll_interval_s=poll_interval_s,
    )


async def _drive(
    store: FakeStore, blobs: LocalBlobStore, execute: ExecuteFn, *run_ids: str, **kw: Any
) -> None:
    hub = EventHub()
    worker = _worker(store, blobs, hub, execute, **kw)
    await worker.start()
    try:
        await _until(_done(store, *run_ids))
    finally:
        await worker.stop()


# --------------------------------------------------------------------------------------------
# Events: store first, then hub
# --------------------------------------------------------------------------------------------


def test_worker_events_land_in_the_store_in_seq_order_and_reach_a_subscriber(
    blobs: LocalBlobStore,
) -> None:
    store = FakeStore()
    store.add("r1")
    _upload(blobs, "r1")

    async def go() -> list[int]:
        hub = EventHub()
        worker = _worker(store, blobs, hub, scripted(happy("r1")))
        async with hub.subscribe("r1") as live:
            await worker.start()
            try:
                received = [e.seq async for e in live]
            finally:
                await worker.stop()
        return received

    received = _run(go)
    assert [e.seq for e in store.events_after("r1", 0)] == [1, 2, 3]
    assert store.types("r1") == ["run_started", "rules_applied", "run_finished"]
    assert received == [1, 2, 3]


def test_worker_publishes_an_event_only_after_it_is_stored(blobs: LocalBlobStore) -> None:
    store = FakeStore()
    store.add("r1")
    _upload(blobs, "r1")
    stored_when_published: list[bool] = []

    async def go() -> None:
        class CheckingHub(EventHub):
            def publish(self, run_id: str, event: EventModel) -> None:
                stored = [e.seq for e in store.events_after(run_id, 0)]
                stored_when_published.append(event.seq in stored)
                super().publish(run_id, event)

        hub = CheckingHub()
        worker = _worker(store, blobs, hub, scripted(happy("r1")))
        await worker.start()
        try:
            await _until(_done(store, "r1"))
        finally:
            await worker.stop()

    _run(go)
    assert stored_when_published == [True, True, True]


def test_worker_run_started_carries_the_uploaded_file_name(blobs: LocalBlobStore) -> None:
    store = FakeStore()
    store.add("r1")
    _upload(blobs, "r1")
    _run(lambda: _drive(store, blobs, scripted(happy("r1")), "r1"))
    first = store.events_after("r1", 0)[0]
    assert isinstance(first, ev.RunStarted)
    assert first.file_name == "r1_orders.csv"


def test_worker_hub_subscriptions_close_after_the_run(blobs: LocalBlobStore) -> None:
    store = FakeStore()
    store.add("r1")
    _upload(blobs, "r1")

    async def go() -> int:
        hub = EventHub()
        worker = _worker(store, blobs, hub, scripted(happy("r1")))
        await worker.start()
        try:
            await _until(_done(store, "r1"))
            await asyncio.sleep(0.05)
            return hub.subscriber_count("r1")
        finally:
            await worker.stop()

    assert _run(go) == 0


# --------------------------------------------------------------------------------------------
# Progress (contract "Run summaries")
# --------------------------------------------------------------------------------------------


def test_worker_cost_usd_grows_with_node_events_and_ends_at_run_finished_cost(
    blobs: LocalBlobStore,
) -> None:
    store = FakeStore()
    store.add("r1")
    _upload(blobs, "r1")
    events: list[EventModel] = [
        started("r1", rows=48),
        attempt_failed("n1", 0.01),
        node_finished("n1", 0.02),
        node_finished("n2", 0.03),
        finished(cost=0.10),
    ]
    _run(lambda: _drive(store, blobs, scripted(events), "r1"))

    costs = [c for (_r, _rows, c) in store.progress if c is not None]
    assert costs == pytest.approx([0.01, 0.03, 0.06])
    assert [rows for (_r, rows, _c) in store.progress if rows is not None] == [48]
    assert store.finished == [("r1", "succeeded", 0.10)]
    row = store.get_run("r1")
    assert row is not None and row.cost_usd == pytest.approx(0.10) and row.rows == 48


def test_worker_finishes_with_the_status_of_run_finished(blobs: LocalBlobStore) -> None:
    store = FakeStore()
    store.add("r1")
    _upload(blobs, "r1")
    events = [started("r1"), finished("quarantined", 0.5)]
    _run(lambda: _drive(store, blobs, scripted(events), "r1"))
    assert store.finished == [("r1", "quarantined", 0.5)]


# --------------------------------------------------------------------------------------------
# Exactly one terminal event (Ruling R4)
# --------------------------------------------------------------------------------------------


def test_worker_execute_that_raises_yields_exactly_one_failed_run_finished(
    blobs: LocalBlobStore,
) -> None:
    store = FakeStore()
    store.add("r1")
    _upload(blobs, "r1")
    events: list[EventModel] = [started("r1"), node_finished("n1", 0.04)]
    _run(lambda: _drive(store, blobs, scripted(events, raises=KeyError("secret")), "r1"))

    (terminal,) = store.terminals("r1")
    assert (terminal.status, terminal.summary, terminal.tokens) == ("failed", "KeyError", 0)
    assert terminal.cost_usd == pytest.approx(0.04)
    assert [e.seq for e in store.events_after("r1", 0)] == [1, 2, 3]
    assert store.finished == [("r1", "failed", pytest.approx(0.04))]


def test_worker_does_not_add_a_terminal_when_execute_already_emitted_one(
    blobs: LocalBlobStore,
) -> None:
    store = FakeStore()
    store.add("r1")
    _upload(blobs, "r1")
    events = [started("r1"), finished("failed", 0.2)]
    _run(lambda: _drive(store, blobs, scripted(events, raises=RuntimeError("x")), "r1"))

    assert len(store.terminals("r1")) == 1
    assert store.finished == [("r1", "failed", 0.2)]


def test_worker_store_failure_on_one_event_leaves_no_gap_and_one_terminal(
    blobs: LocalBlobStore,
) -> None:
    store = FakeStore()
    store.add("r1")
    _upload(blobs, "r1")
    store.fail_append = lambda e: e.type == "rules_applied"  # type: ignore[attr-defined]
    # The real execute_run: the emit failure surfaces inside its try, so it emits
    # run_finished(failed) itself and re-raises.
    _run(
        lambda: _drive(
            store,
            blobs,
            _real_execute(RulesOnlySystem().run, _Loads()),
            "r1",
        )
    )
    assert store.types("r1") == ["run_started", "run_finished"]
    assert [e.seq for e in store.events_after("r1", 0)] == [1, 2]
    (terminal,) = store.terminals("r1")
    assert terminal.status == "failed"


def test_worker_failed_terminal_append_falls_back_to_its_own_run_finished(
    blobs: LocalBlobStore,
) -> None:
    store = FakeStore()
    store.add("r1")
    _upload(blobs, "r1")
    calls = {"n": 0}

    def fail_first_terminal(event: EventModel) -> bool:
        if event.type == "run_finished":  # type: ignore[attr-defined]
            calls["n"] += 1
            return calls["n"] == 1
        return False

    store.fail_append = fail_first_terminal
    _run(lambda: _drive(store, blobs, scripted(happy("r1")), "r1"))

    (terminal,) = store.terminals("r1")
    assert (terminal.status, terminal.summary) == ("failed", "ConnectionError")
    assert [e.seq for e in store.events_after("r1", 0)] == [1, 2, 3]
    assert store.finished == [("r1", "failed", 0.0)]


def test_worker_terminal_adopted_on_resync_reaches_live_subscribers_once(
    blobs: LocalBlobStore,
) -> None:
    store = FakeStore()
    store.add("r1")
    _upload(blobs, "r1")
    calls = {"n": 0}

    def first_terminal_commits_then_fails(event: EventModel) -> bool:
        if event.type == "run_finished":  # type: ignore[attr-defined]
            calls["n"] += 1
            return calls["n"] == 1
        return False

    store.commit_then_fail = first_terminal_commits_then_fails

    async def go() -> list[tuple[int, str]]:
        hub = EventHub()
        worker = _worker(store, blobs, hub, scripted(happy("r1")))
        async with hub.subscribe("r1") as live:
            await worker.start()
            try:
                received = [(e.seq, e.type) async for e in live]  # type: ignore[attr-defined]
            finally:
                await worker.stop()
        return received

    received = _run(go)
    assert received == [(1, "run_started"), (2, "rules_applied"), (3, "run_finished")]
    (terminal,) = store.terminals("r1")
    assert terminal.status == "succeeded"  # the stored terminal was adopted, not replaced
    assert store.finished == [("r1", "succeeded", 0.0)]


def test_worker_run_closed_after_its_run_finished_still_finishes_the_run(
    blobs: LocalBlobStore,
) -> None:
    store = FakeStore()
    store.add("r1")
    _upload(blobs, "r1")
    events = [*happy("r1"), ev.RulesApplied(actions=["late"])]  # refused: the run is closed
    _run(lambda: _drive(store, blobs, scripted(events), "r1"))

    assert store.types("r1") == ["run_started", "rules_applied", "run_finished"]
    assert store.finished == [("r1", "succeeded", 0.0)]


def test_worker_missing_upload_still_ends_with_one_failed_run_finished(
    blobs: LocalBlobStore,
) -> None:
    store = FakeStore()
    store.add("r1")  # no blob uploaded
    _run(lambda: _drive(store, blobs, scripted(happy("r1")), "r1"))

    (terminal,) = store.terminals("r1")
    assert (terminal.seq, terminal.status, terminal.summary) == (1, "failed", "FileNotFoundError")
    assert store.finished == [("r1", "failed", 0.0)]


def test_worker_stop_closes_an_in_flight_run_with_one_failed_run_finished(
    blobs: LocalBlobStore,
) -> None:
    store = FakeStore()
    store.add("r1")
    _upload(blobs, "r1")
    entered, release = threading.Event(), threading.Event()

    def block() -> None:
        entered.set()
        release.wait(5)

    events: list[EventModel] = [started("r1"), node_finished("n1", 0.07), finished()]

    def emit_start_then_block(path: Path, table: str, *, run_id: str, emit: Any, **kw: Any) -> Any:
        log = EventLog()
        emit(log.emit(events[0]))
        emit(log.emit(events[1]))
        block()
        emit(log.emit(events[2]))  # after the worker closed the run: refused
        raise AssertionError("unreachable: the closed run refuses further events")

    async def go() -> None:
        hub = EventHub()
        worker = _worker(store, blobs, hub, emit_start_then_block)
        await worker.start()
        await asyncio.to_thread(entered.wait, 5)
        try:
            await worker.stop(timeout_s=0.05)
        finally:
            release.set()
        await asyncio.sleep(0.1)  # the released thread tries to emit; it must be refused

    _run(go)
    (terminal,) = store.terminals("r1")
    assert (terminal.status, terminal.summary, terminal.tokens) == ("failed", "CancelledError", 0)
    assert terminal.cost_usd == pytest.approx(0.07)
    assert store.finished == [("r1", "failed", pytest.approx(0.07))]


def test_worker_stop_waits_for_a_run_that_ends_within_the_grace_period(
    blobs: LocalBlobStore,
) -> None:
    store = FakeStore()
    store.add("r1")
    _upload(blobs, "r1")
    entered = threading.Event()

    def slow() -> None:
        entered.set()
        time.sleep(0.1)

    async def go() -> None:
        hub = EventHub()
        worker = _worker(store, blobs, hub, scripted(happy("r1"), before=slow))
        await worker.start()
        await asyncio.to_thread(entered.wait, 5)
        await worker.stop(timeout_s=5)

    _run(go)
    assert store.finished == [("r1", "succeeded", 0.0)]


# --------------------------------------------------------------------------------------------
# Scheduling
# --------------------------------------------------------------------------------------------


def test_worker_two_queued_runs_with_concurrency_one_run_one_after_the_other(
    blobs: LocalBlobStore,
) -> None:
    store = FakeStore()
    spans: dict[str, list[float]] = {}
    lock = threading.Lock()
    for run_id in ("r1", "r2"):
        store.add(run_id)
        _upload(blobs, run_id)

    def make(run_id: str) -> Callable[[], None]:
        def mark() -> None:
            with lock:
                spans[run_id] = [time.monotonic()]
            time.sleep(0.05)
            with lock:
                spans[run_id].append(time.monotonic())

        return mark

    def execute(path: Path, table: str, *, run_id: str, emit: Any, **kw: Any) -> RunOutcome:
        return scripted(happy(run_id), before=make(run_id))(path, table, run_id=run_id, emit=emit)

    _run(lambda: _drive(store, blobs, execute, "r1", "r2", concurrency=1))

    assert [r for (r, _s, _c) in store.finished] == ["r1", "r2"]
    assert spans["r1"][1] <= spans["r2"][0]


def test_worker_concurrency_two_runs_two_at_once(blobs: LocalBlobStore) -> None:
    store = FakeStore()
    barrier = threading.Barrier(2, timeout=5)
    for run_id in ("r1", "r2"):
        store.add(run_id)
        _upload(blobs, run_id)

    def execute(path: Path, table: str, *, run_id: str, emit: Any, **kw: Any) -> RunOutcome:
        barrier.wait()  # both runs must be in flight together
        return scripted(happy(run_id))(path, table, run_id=run_id, emit=emit)

    _run(lambda: _drive(store, blobs, execute, "r1", "r2", concurrency=2))
    assert sorted(r for (r, s, _c) in store.finished if s == "succeeded") == ["r1", "r2"]


def test_worker_notify_wakes_an_idle_worker_immediately(blobs: LocalBlobStore) -> None:
    store = FakeStore()

    async def go() -> float:
        hub = EventHub()
        worker = _worker(store, blobs, hub, scripted(happy("r1")), poll_interval_s=30)
        await worker.start()
        try:
            await asyncio.sleep(0.05)  # idle: waiting for 30 s or a notify
            store.add("r1")
            _upload(blobs, "r1")
            begin = time.monotonic()
            worker.notify()
            await _until(_done(store, "r1"), timeout_s=3)
            return time.monotonic() - begin
        finally:
            await worker.stop()

    assert _run(go) < 2


def test_worker_idle_polls_the_store(blobs: LocalBlobStore) -> None:
    store = FakeStore()

    async def go() -> None:
        hub = EventHub()
        worker = _worker(store, blobs, hub, scripted(happy("r1")), poll_interval_s=0.02)
        await worker.start()
        try:
            await asyncio.sleep(0.05)
            store.add("r1")  # no notify
            _upload(blobs, "r1")
            await _until(_done(store, "r1"), timeout_s=3)
        finally:
            await worker.stop()

    _run(go)
    assert store.finished == [("r1", "succeeded", 0.0)]


def test_worker_survives_a_store_outage_while_claiming(blobs: LocalBlobStore) -> None:
    store = FakeStore()
    store.add("r1")
    _upload(blobs, "r1")
    real_claim = store.claim_next
    calls = {"n": 0}

    def flaky() -> RunRow | None:
        calls["n"] += 1
        if calls["n"] <= 2:
            raise ConnectionError("down")
        return real_claim()

    store.claim_next = flaky  # type: ignore[method-assign]
    _run(lambda: _drive(store, blobs, scripted(happy("r1")), "r1"))
    assert store.finished == [("r1", "succeeded", 0.0)]


def test_worker_rejects_a_concurrency_below_one(blobs: LocalBlobStore) -> None:
    async def go() -> None:
        _worker(FakeStore(), blobs, EventHub(), scripted([]), concurrency=0)

    with pytest.raises(ValueError):
        _run(go)


# --------------------------------------------------------------------------------------------
# Artefacts: preview and transforms
# --------------------------------------------------------------------------------------------


class _Loads:
    """A ``load_fn`` that pretends to write a snapshot."""

    def __call__(self, out: SystemOutput, contract: Contract, **kw: Any) -> LoadResult:
        return LoadResult(
            snapshot_id="42",
            rows_loaded=out.df.height,
            rows_quarantined=len(out.quarantined),
            skipped=False,
        )


def _real_execute(run_fn: Any, loads: _Loads) -> ExecuteFn:
    def execute(path: Path, table: str, *, run_id: str, emit: Any, **kw: Any) -> RunOutcome:
        return execute_run(
            path,
            table,
            run_id=run_id,
            emit=emit,
            run_fn=run_fn,
            load_fn=loads,
            loaded_fn=lambda *_a, **_k: None,
            settings_fn=lambda: object(),  # type: ignore[arg-type, return-value]
            reference_fn=lambda _t: {},
        )

    return execute


def test_worker_loaded_run_stores_a_preview_and_its_key(
    blobs: LocalBlobStore, orders_df: pl.DataFrame, tmp_path: Path
) -> None:
    store = FakeStore()
    store.add("r1")
    csv = tmp_path / "orders.csv"
    orders_df.head(20).write_csv(csv)
    _upload(blobs, "r1", csv.read_bytes())

    _run(lambda: _drive(store, blobs, _real_execute(RulesOnlySystem().run, _Loads()), "r1"))

    assert "loaded" in store.types("r1")
    artefacts = store.artefacts["r1"]
    assert artefacts.preview_key == "runs/r1/preview.parquet"
    preview = pl.read_parquet(blobs.get_bytes("runs/r1/preview.parquet"))
    assert preview.height == 20
    assert {"pk", "quarantined", "reason", "cells"} <= set(preview.columns)


def test_worker_run_without_loaded_has_no_preview(blobs: LocalBlobStore) -> None:
    store = FakeStore()
    store.add("r1")
    _upload(blobs, "r1")
    _run(lambda: _drive(store, blobs, scripted(happy("r1")), "r1"))
    assert store.artefacts["r1"].preview_key is None
    with pytest.raises(FileNotFoundError):
        blobs.get_bytes("runs/r1/preview.parquet")


def _transform(node_id: str, attempt: int, passed: bool) -> dict[str, Any]:
    return {
        "transform_id": f"r1-{node_id}-{attempt}",
        "node_id": node_id,
        "attempt": attempt,
        "code": "def transform(s): return s",
        "model": "small",
        "passed": passed,
    }


def test_worker_saves_transforms_in_plan_order_then_attempt(blobs: LocalBlobStore) -> None:
    store = FakeStore()
    store.add("r1")
    _upload(blobs, "r1")
    plan = ev.PlanCreated(
        nodes=[
            ev.PlanNode(id="n2", title="b", columns=["b"], tier="small", depends_on=[]),
            ev.PlanNode(id="n1", title="a", columns=["a"], tier="small", depends_on=[]),
        ]
    )
    output = SystemOutput(
        df=pl.DataFrame(),
        flags=set(),
        quarantined=set(),
        file_quarantined=False,
        cost_usd=0.0,
        latency_s=0.0,
        meta={
            "transforms": [
                _transform("n1", 2, True),
                _transform("n2", 1, True),
                _transform("n1", 1, False),
            ]
        },
    )
    events: list[EventModel] = [started("r1"), plan, finished()]
    _run(lambda: _drive(store, blobs, scripted(events, output=output), "r1"))

    saved = store.artefacts["r1"].transforms
    assert [(t["node_id"], t["attempt"]) for t in saved] == [("n2", 1), ("n1", 1), ("n1", 2)]
    assert saved[0]["transform_id"] == "r1-n2-1"


# --------------------------------------------------------------------------------------------
# Startup recovery (Review Focus 2)
# --------------------------------------------------------------------------------------------


def _stamp(event: EventModel, seq: int) -> EventModel:
    return event.model_copy(update={"seq": seq, "at": CREATED})


def test_recover_closes_an_interrupted_run_with_exactly_one_failed_run_finished(
    blobs: LocalBlobStore,
) -> None:
    store = FakeStore()
    store.add("r1", status="running", cost=0.25)
    store.append_event("r1", _stamp(started("r1"), 1))
    store.append_event("r1", _stamp(node_finished("n1", 0.25), 2))

    async def go() -> None:
        await _worker(store, blobs, EventHub(), scripted([])).recover()

    _run(go)
    (terminal,) = store.terminals("r1")
    assert terminal.seq == 3
    assert (terminal.status, terminal.summary, terminal.tokens) == ("failed", RESTART_SUMMARY, 0)
    assert RESTART_SUMMARY == "the server restarted during the run"
    assert terminal.cost_usd == pytest.approx(0.25)
    assert terminal.at.endswith("Z")
    assert store.finished == [("r1", "failed", 0.25)]


def test_recover_run_already_holding_run_finished_is_only_finished(
    blobs: LocalBlobStore,
) -> None:
    store = FakeStore()
    store.add("r1", status="running")
    store.append_event("r1", _stamp(started("r1"), 1))
    store.append_event("r1", _stamp(finished("quarantined", 0.3), 2))

    async def go() -> None:
        await _worker(store, blobs, EventHub(), scripted([])).recover()

    _run(go)
    assert len(store.terminals("r1")) == 1
    assert store.finished == [("r1", "quarantined", 0.3)]


def test_recover_leaves_queued_and_finished_runs_alone(blobs: LocalBlobStore) -> None:
    store = FakeStore()
    store.add("q1")
    store.add("done", status="succeeded")

    async def go() -> None:
        await _worker(store, blobs, EventHub(), scripted([])).recover()

    _run(go)
    assert store.finished == []
    assert store.events_after("q1", 0) == [] and store.events_after("done", 0) == []


def test_recover_closes_every_interrupted_run(blobs: LocalBlobStore) -> None:
    store = FakeStore()
    store.add("a", status="running")
    store.add("b", status="running")

    async def go() -> None:
        await _worker(store, blobs, EventHub(), scripted([])).recover()

    _run(go)
    assert sorted(r for (r, _s, _c) in store.finished) == ["a", "b"]
    assert [e.seq for e in store.terminals("a")] == [1]
