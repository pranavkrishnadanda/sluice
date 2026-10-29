"""Shared fakes for API tests: an in-memory run store and scripted ``execute`` functions."""

from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from sluice.agent import events as ev
from sluice.agent.events import EventLog, EventModel
from sluice.api.store import (
    TERMINAL_STATUSES,
    Artefacts,
    DuplicateEvent,
    RunNotActive,
    RunNotFound,
    RunRow,
)
from sluice.evals.types import SystemOutput
from sluice.runs.execute import RunOutcome

SHA = "c" * 64
CREATED = "2026-10-27T10:14:00.000Z"


class FakeStore:
    """In-memory ``RunStore``: same validation of seq uniqueness and terminal status."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.runs: dict[str, RunRow] = {}
        self.events: dict[str, list[EventModel]] = {}
        self.artefacts: dict[str, Artefacts] = {}
        self.progress: list[tuple[str, int | None, float | None]] = []
        self.finished: list[tuple[str, str, float]] = []
        self.fail_append: Callable[[EventModel], bool] = lambda _e: False
        self.commit_then_fail: Callable[[EventModel], bool] = lambda _e: False
        """Store the event, then raise as if the connection dropped before the commit ack."""
        self.fail_transform = False
        self.fail_save = False

    def add(self, run_id: str, *, status: str = "queued", cost: float = 0.0) -> None:
        self.runs[run_id] = RunRow(
            id=run_id,
            file_name=f"{run_id}_orders.csv",
            table="orders",
            rows=None,
            status=status,  # type: ignore[arg-type]
            created_at=CREATED,
            cost_usd=cost,
            blob_key=f"uploads/{run_id}/input.csv",
            file_sha256=SHA,
            started_at=CREATED if status == "running" else None,
            finished_at=None,
        )
        self.events[run_id] = []

    def claim_next(self) -> RunRow | None:
        with self.lock:
            for run_id, row in self.runs.items():
                if row.status == "queued":
                    self.runs[run_id] = row.model_copy(update={"status": "running"})
                    return self.runs[run_id]
        return None

    def interrupted(self) -> list[RunRow]:
        with self.lock:
            return [r for r in self.runs.values() if r.status == "running"]

    def append_event(self, run_id: str, event: EventModel) -> None:
        with self.lock:
            if run_id not in self.runs:
                raise RunNotFound(run_id)
            if self.fail_append(event):
                raise ConnectionError("postgres is down")
            if event.seq < 1 or not event.at:
                raise ValueError("unstamped")
            if any(e.seq == event.seq for e in self.events[run_id]):
                raise DuplicateEvent(run_id)
            self.events[run_id].append(event)
            if self.commit_then_fail(event):
                raise ConnectionError("connection lost after commit")

    def events_after(self, run_id: str, after_seq: int) -> list[EventModel]:
        with self.lock:
            return sorted(
                (e for e in self.events.get(run_id, []) if e.seq > after_seq),
                key=lambda e: e.seq,
            )

    def update_progress(
        self, run_id: str, *, rows: int | None = None, cost_usd: float | None = None
    ) -> None:
        with self.lock:
            self.progress.append((run_id, rows, cost_usd))
            row = self.runs[run_id]
            update: dict[str, Any] = {}
            if rows is not None:
                update["rows"] = rows
            if cost_usd is not None:
                update["cost_usd"] = cost_usd
            self.runs[run_id] = row.model_copy(update=update)

    def finish(self, run_id: str, status: str, cost_usd: float) -> None:
        with self.lock:
            row = self.runs.get(run_id)
            if row is None:
                raise RunNotFound(run_id)
            if row.status in TERMINAL_STATUSES:
                raise RunNotActive(run_id)
            self.runs[run_id] = row.model_copy(update={"status": status, "cost_usd": cost_usd})
            self.finished.append((run_id, status, cost_usd))

    def append_transform(self, run_id: str, record: dict[str, Any]) -> None:
        """Like ``RunStore.append_transform``: append once per ``transform_id``."""
        with self.lock:
            if self.fail_transform:
                raise ConnectionError("postgres is down")
            if run_id not in self.runs:
                raise RunNotFound(run_id)
            current = self.artefacts.get(run_id) or Artefacts(preview_key=None)
            if any(t["transform_id"] == record["transform_id"] for t in current.transforms):
                return
            self.artefacts[run_id] = current.model_copy(
                update={"transforms": [*current.transforms, dict(record)]}
            )

    def save_artefacts(
        self, run_id: str, *, preview_key: str | None, transforms: list[dict[str, Any]]
    ) -> None:
        """Like ``RunStore.save_artefacts``: ``transforms`` win by id, others are kept."""
        with self.lock:
            if self.fail_save:
                raise ConnectionError("postgres is down")
            ids = {t.get("transform_id") for t in transforms}
            current = self.artefacts.get(run_id)
            kept = [] if current is None else current.transforms
            merged = [*transforms, *(t for t in kept if t.get("transform_id") not in ids)]
            self.artefacts[run_id] = Artefacts(
                preview_key=preview_key, transforms=merged, saved=True
            )

    def get_artefacts(self, run_id: str) -> Artefacts | None:
        with self.lock:
            return self.artefacts.get(run_id)

    def get_run(self, run_id: str) -> RunRow | None:
        with self.lock:
            return self.runs.get(run_id)

    # helpers -------------------------------------------------------------------------------

    def types(self, run_id: str) -> list[str]:
        return [e.type for e in self.events_after(run_id, 0)]  # type: ignore[attr-defined]

    def terminals(self, run_id: str) -> list[ev.RunFinished]:
        return [e for e in self.events_after(run_id, 0) if isinstance(e, ev.RunFinished)]


ExecuteFn = Callable[..., RunOutcome]


def scripted(
    events: list[EventModel],
    *,
    raises: BaseException | None = None,
    output: SystemOutput | None = None,
    before: Callable[[], None] | None = None,
) -> ExecuteFn:
    """An ``execute`` that stamps ``events`` through a real ``EventLog`` and emits them."""

    def execute(path: Path, table: str, *, run_id: str, emit: Any, **_kw: Any) -> RunOutcome:
        assert path.is_file()
        if before is not None:
            before()
        log = EventLog()
        final: ev.RunFinished | None = None
        for event in events:
            stamped = log.emit(event)
            emit(stamped)
            if isinstance(stamped, ev.RunFinished):
                final = stamped
        if raises is not None:
            raise raises
        assert final is not None
        return RunOutcome(
            result=None, status=final.status, output=output, cost_usd=final.cost_usd, tokens=0
        )

    return execute


def started(run_id: str, rows: int | None = 40) -> ev.RunStarted:
    return ev.RunStarted(run_id=run_id, file_name="blob.csv", table="orders", rows=rows)


def node_finished(node_id: str, cost: float) -> ev.NodeFinished:
    return ev.NodeFinished(
        node_id=node_id,
        attempt=1,
        tier="small",
        tokens=10,
        cost_usd=cost,
        latency_ms=5,
        transform_id=f"t-{node_id}",
    )


def attempt_failed(node_id: str, cost: float) -> ev.AttemptFailed:
    return ev.AttemptFailed(
        node_id=node_id, attempt=1, max_attempts=3, error="boom", tokens=5, cost_usd=cost
    )


def finished(status: str = "succeeded", cost: float = 0.0) -> ev.RunFinished:
    return ev.RunFinished(status=status, summary="done", cost_usd=cost, tokens=99)  # type: ignore[arg-type]


def happy(run_id: str) -> list[EventModel]:
    return [started(run_id), ev.RulesApplied(actions=[]), finished()]
