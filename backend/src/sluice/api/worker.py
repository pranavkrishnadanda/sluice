"""The in-process job worker: runs queued uploads and guarantees one ``run_finished`` per run.

Per run (each step blocking, so the whole run happens in one ``asyncio.to_thread`` call):

1. ``store.claim_next()`` moves the oldest queued run to ``running`` (``SKIP LOCKED``);
2. ``blobs.open_local(blob_key)`` gives a local copy of the upload;
3. ``execute`` (:func:`sluice.runs.execute.execute_run`) runs it, with ``emit`` = append to the
   store **then** publish to the hub, so a replay from the store never misses an event the hub
   delivered. ``emit`` also renumbers ``seq`` from the store's point of view (gapless even when
   an append failed) and rewrites ``run_started.fileName`` to the uploaded file's name (the
   local copy is called ``blob.<ext>``). For ``run_finished`` it also calls ``store.finish``
   between the append and the publish, so a client that refetches ``GET /runs`` on receiving
   it sees the terminal status. ``on_transform`` appends each transform record to the store
   (``RunStore.append_transform``) as its attempt ends, before the event announcing it, so
   every delivered ``node_finished.transformId`` resolves through ``GET /runs/{id}/transforms``;
   a failed append is logged and repaired by the end-of-run save (step 5);
4. progress: ``rows`` from ``run_started``; ``cost_usd`` is the running total of every
   ``attempt_failed`` / ``node_finished``, set to ``run_finished.costUsd`` at the end
   (contract "Run summaries");
5. artefacts: when the run emitted ``loaded``, the upload is read once more (accepted trade-off:
   :class:`~sluice.runs.execute.RunOutcome` carries no raw frame) to build the before/after
   preview, stored at ``runs/<id>/preview.parquet``; the transforms (``meta["transforms"]``,
   each with its ``transform_id``) are reconciled with the live ones by id (none dropped or
   duplicated), in plan order, then attempt. This happens after ``run_finished`` is out: until
   the key is saved the preview route answers ``409 not_ready``.

Exactly one terminal event (Ruling R4): the worker records whether a ``run_finished`` was
appended. On any exception with none recorded it appends ``run_finished(failed,
summary=<exception type name>, costUsd=<running total>, tokens=0)`` itself. On cancellation
(:meth:`Worker.stop` after its grace period) it closes the run the same way and re-raises; the
run's thread cannot be interrupted, but every later ``emit`` from it is refused
(:class:`RunClosed`), so no second terminal can appear. If no ``run_finished`` could be stored
at all (the store is down), the run is left ``running`` for :meth:`Worker.recover` on the next
start: a terminal ``api_runs.status`` always has its ``run_finished``.

Summaries and logs name exception types only: messages can carry DSNs or host paths.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import logging
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from sluice.agent.events import (
    AttemptFailed,
    EventModel,
    Loaded,
    NodeFinished,
    PlanCreated,
    RunFinished,
    RunStarted,
)
from sluice.api.blobs import BlobStore
from sluice.api.hub import EventHub
from sluice.api.store import DuplicateEvent, RunNotActive, RunRow, RunStore, iso_utc
from sluice.contracts.model import load_contract
from sluice.io import read_input
from sluice.runs.execute import RunOutcome, execute_run
from sluice.runs.preview import build_preview
from sluice.settings import Settings, get_settings

__all__ = ["POLL_INTERVAL_S", "RESTART_SUMMARY", "STOP_GRACE_S", "RunClosed", "Worker"]

_log = logging.getLogger(__name__)

POLL_INTERVAL_S: Final = 0.5
"""Idle wait between ``claim_next`` calls; :meth:`Worker.notify` cuts it short."""
STOP_GRACE_S: Final = 10.0
"""How long :meth:`Worker.stop` lets in-flight runs finish before cancelling them."""
RESTART_SUMMARY: Final = "the server restarted during the run"

ExecuteFn = Callable[..., RunOutcome]


class RunClosed(Exception):
    """The worker already closed this run; its thread may not emit any more events."""


def _now() -> str:
    return iso_utc(datetime.now(UTC))


def _type_name(exc: BaseException) -> str:
    return type(exc).__name__


def preview_key(run_id: str) -> str:
    """Blob key of the run's before/after preview."""
    return f"runs/{run_id}/preview.parquet"


class _RunState:
    """One run's bookkeeping, shared by the run's thread and the cancelling coroutine."""

    __slots__ = (
        "closed",
        "cost",
        "file_name",
        "finished",
        "finished_stored",
        "loaded",
        "lock",
        "plan",
        "seq",
    )

    def __init__(self, file_name: str) -> None:
        self.lock = threading.Lock()
        self.file_name = file_name
        self.seq = 0
        """Last ``seq`` stored for the run."""
        self.cost = 0.0
        self.closed = False
        self.loaded = False
        self.finished: RunFinished | None = None
        self.finished_stored = False
        """``store.finish`` succeeded for :attr:`finished` (or the run was already finished)."""
        self.plan: list[str] = []


class Worker:
    """Runs queued uploads, ``concurrency`` at a time, on the app's event loop.

    Args:
        store: The run store (blocking; always called through ``asyncio.to_thread``).
        blobs: Where uploads and previews live.
        hub: Live fan-out to SSE subscribers.
        concurrency: Runs executed at once (1..4, ``ApiSettings.concurrency``).
        execute: The execution core (:func:`~sluice.runs.execute.execute_run`); injectable.
        settings_fn: Service settings, handed to ``execute``.
        poll_interval_s: Idle wait between claims.
    """

    def __init__(
        self,
        store: RunStore,
        blobs: BlobStore,
        hub: EventHub,
        *,
        concurrency: int,
        execute: ExecuteFn = execute_run,
        settings_fn: Callable[[], Settings] = get_settings,
        poll_interval_s: float = POLL_INTERVAL_S,
    ) -> None:
        if concurrency < 1:
            raise ValueError("concurrency must be >= 1")
        if poll_interval_s <= 0:
            raise ValueError("poll_interval_s must be > 0")
        self._store = store
        self._blobs = blobs
        self._hub = hub
        self._concurrency = concurrency
        self._execute = execute
        self._settings_fn = settings_fn
        self._poll_s = poll_interval_s
        self._wake = asyncio.Event()
        self._stopping = False
        self._slots: list[asyncio.Task[None]] = []
        self._busy: set[asyncio.Task[None]] = set()

    # ---- lifecycle -------------------------------------------------------------------------

    async def start(self) -> None:
        """Start ``concurrency`` slots that claim and run queued runs until :meth:`stop`."""
        if self._slots:
            return
        self._stopping = False
        self._slots = [
            asyncio.create_task(self._slot(), name=f"sluice-worker-{i}")
            for i in range(self._concurrency)
        ]

    async def stop(self, timeout_s: float = STOP_GRACE_S) -> None:
        """Stop claiming; let in-flight runs finish for ``timeout_s``, then cancel them.

        A cancelled run is closed with ``run_finished(failed, "CancelledError")``.
        """
        self._stopping = True
        self._wake.set()
        slots, self._slots = self._slots, []
        if not slots:
            return
        _done, pending = await asyncio.wait(slots, timeout=timeout_s)
        for task in pending:
            task.cancel()
        await asyncio.gather(*slots, return_exceptions=True)

    def notify(self) -> None:
        """Wake idle slots now (a run was queued). Call on the event loop."""
        self._wake.set()

    async def recover(self) -> None:
        """Close every run left ``running`` by a crash or restart (call before :meth:`start`).

        Appends ``run_finished(failed, RESTART_SUMMARY, costUsd=<stored>, tokens=0)`` with the
        next ``seq``, then finishes the run ``failed``. A run whose last stored event is already
        its ``run_finished`` (the process died before ``finish``) is only finished, with that
        event's status and cost.
        """
        for row in await asyncio.to_thread(self._store.interrupted):
            try:
                await asyncio.to_thread(self._recover_one, row)
            except Exception as exc:
                _log.error("recovery of run %s failed: %s", row.id, _type_name(exc))

    def _recover_one(self, row: RunRow) -> None:
        events = self._store.events_after(row.id, 0)
        last = events[-1] if events else None
        if isinstance(last, RunFinished):
            self._store.finish(row.id, last.status, last.cost_usd)
            self._settle(row.id)
            return
        terminal = RunFinished(
            status="failed",
            summary=RESTART_SUMMARY,
            cost_usd=row.cost_usd,
            tokens=0,
            seq=(last.seq if last else 0) + 1,
            at=_now(),
        )
        self._store.append_event(row.id, terminal)
        self._store.finish(row.id, "failed", row.cost_usd)
        self._settle(row.id)

    # ---- scheduling ------------------------------------------------------------------------

    async def _slot(self) -> None:
        while not self._stopping:
            self._wake.clear()
            try:
                row = await asyncio.to_thread(self._store.claim_next)
            except Exception as exc:
                _log.error("claiming the next run failed: %s", _type_name(exc))
                row = None
            if row is None:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._wake.wait(), self._poll_s)
                continue
            await self._run(row)

    async def _run(self, row: RunRow) -> None:
        state = _RunState(row.file_name)
        try:
            await asyncio.to_thread(self._run_sync, row, state)
        except asyncio.CancelledError:
            closed_here = await asyncio.to_thread(
                self._close_failed, row.id, state, "CancelledError"
            )
            # Settle only a run this coroutine closed: after the run's own run_finished its
            # thread may still be saving the real preview, which must not be overwritten.
            if closed_here:
                await asyncio.to_thread(self._settle, row.id)
            raise
        finally:
            self._hub.close(row.id)

    # ---- one run (in a worker thread) ------------------------------------------------------

    def _run_sync(self, row: RunRow, state: _RunState) -> None:
        outcome: RunOutcome | None = None
        try:
            with self._blobs.open_local(row.blob_key) as path:
                outcome = self._execute(
                    path,
                    row.table,
                    run_id=row.id,
                    emit=lambda event: self._emit(row.id, state, event),
                    on_transform=lambda record: self._on_transform(row.id, state, record),
                    settings_fn=self._settings_fn,
                )
                artefacts = self._artefacts(row, state, path, outcome)
        except RunClosed:
            # Refused after the run was closed: by its own run_finished (finish it with that)
            # or by the cancelling coroutine (already finished; RunNotActive is ignored).
            if state.finished is not None:
                self._finish_once(row.id, state)
            self._settle(row.id)
            return
        except Exception as exc:
            _log.warning("run %s raised %s", row.id, _type_name(exc))
            self._close_failed(row.id, state, _type_name(exc))
            self._settle(row.id)
            return
        if state.finished is None:  # execute returned without a stored terminal event
            self._close_failed(row.id, state, "MissingRunFinished")
            self._settle(row.id)
            return
        self._save_and_finish(row.id, state, *artefacts)

    def _emit(self, run_id: str, state: _RunState, event: EventModel) -> None:
        """Store, then publish, one event; renumbered so the stored ``seq`` has no gaps."""
        with state.lock:
            if state.closed:
                raise RunClosed(run_id)
            update: dict[str, Any] = {"seq": state.seq + 1}
            if isinstance(event, RunStarted):
                update["file_name"] = state.file_name
            stamped = event.model_copy(update=update)
            self._store.append_event(run_id, stamped)
            state.seq = stamped.seq
            self._publish(run_id, state, stamped)
            self._track(run_id, state, stamped)

    def _publish(self, run_id: str, state: _RunState, event: EventModel) -> None:
        """Publish a stored event; a ``run_finished`` is first ``store.finish``-ed, so a client
        that refetches ``GET /runs`` on receiving it already sees the terminal status.

        Caller holds ``state.lock``. A failed ``finish`` is logged and retried once the run's
        thread is done; the event is published either way (it is stored).
        """
        if isinstance(event, RunFinished):
            state.finished = event
            self._finish_once(run_id, state)
        self._hub.publish(run_id, event)

    def _on_transform(self, run_id: str, state: _RunState, record: dict[str, Any]) -> None:
        """``on_transform``: persist one transform record before its event is emitted.

        Best effort: a store failure is logged (the end-of-run save carries every record), and
        records from a run already closed are dropped.
        """
        # Read without state.lock: the hook runs on the run's own thread, and a stale False
        # only lets one more (idempotent, harmless) append through after a concurrent close.
        if state.closed:
            return
        try:
            self._store.append_transform(run_id, record)
        except Exception as exc:
            _log.warning("run %s: storing a transform failed: %s", run_id, _type_name(exc))

    def _track(self, run_id: str, state: _RunState, event: EventModel) -> None:
        """Progress and run bookkeeping for a stored event (caller holds ``state.lock``)."""
        rows: int | None = None
        cost: float | None = None
        if isinstance(event, RunStarted):
            rows = event.rows
        elif isinstance(event, AttemptFailed | NodeFinished):
            state.cost += event.cost_usd
            cost = state.cost
        elif isinstance(event, PlanCreated):
            state.plan = [node.id for node in event.nodes]
        elif isinstance(event, Loaded):
            state.loaded = True
        elif isinstance(event, RunFinished):
            state.cost = event.cost_usd
            state.finished = event
            state.closed = True
        if rows is not None or cost is not None:
            try:
                self._store.update_progress(run_id, rows=rows, cost_usd=cost)
            except Exception as exc:  # informational only; the run goes on
                _log.warning("progress update of run %s failed: %s", run_id, _type_name(exc))

    def _close_failed(self, run_id: str, state: _RunState, summary: str) -> bool:
        """Append our own ``run_finished(failed)`` unless one is recorded, then finish.

        Returns ``True`` when this call stored the terminal event (none was recorded before).
        """
        with state.lock:
            state.closed = True
            created = state.finished is None
            if created:
                state.finished = self._append_terminal(run_id, state, summary)
            terminal = state.finished
        if terminal is None:
            _log.error("run %s: no run_finished could be stored; left for recovery", run_id)
            return False
        self._finish_once(run_id, state)
        return created

    def _settle(self, run_id: str) -> None:
        """Mark the artefacts of a run that ends without a preview as saved (best effort).

        Without this a failed, cancelled or recovered run that had emitted ``loaded`` would
        answer ``409 not_ready`` until the route's grace period ran out, instead of
        ``no_preview`` at once. ``save_artefacts`` keeps the transforms appended live.
        """
        try:
            self._store.save_artefacts(run_id, preview_key=None, transforms=[])
        except Exception as exc:
            _log.error("run %s: settling the artefacts failed: %s", run_id, _type_name(exc))

    def _append_terminal(self, run_id: str, state: _RunState, summary: str) -> RunFinished | None:
        """Store and publish ``run_finished(failed)`` at the next ``seq`` (caller holds lock).

        A :class:`DuplicateEvent` means an earlier append committed although it reported an
        error: resync ``seq`` from the store and retry once. The resynced events were stored
        but never published (``state.seq`` only advances after a publish), so they are
        published and tracked (:meth:`_track`: running cost, progress, plan, ``loaded``) now,
        in order, each exactly once; a stored ``run_finished`` among them is adopted as the
        run's terminal event, and the fallback's ``costUsd`` includes the resynced spend.
        """
        for _ in range(2):
            terminal = RunFinished(
                status="failed",
                summary=summary,
                cost_usd=state.cost,
                tokens=0,
                seq=state.seq + 1,
                at=_now(),
            )
            try:
                self._store.append_event(run_id, terminal)
            except DuplicateEvent:
                try:
                    tail = self._store.events_after(run_id, state.seq)
                except Exception as exc:
                    _log.error("run %s: resync failed: %s", run_id, _type_name(exc))
                    return None
                if tail:
                    state.seq = tail[-1].seq
                for event in tail:
                    self._publish(run_id, state, event)
                    self._track(run_id, state, event)  # cost, rows, plan, loaded: as in _emit
                stored = [e for e in tail if isinstance(e, RunFinished)]
                if stored:
                    return stored[-1]
                continue
            except Exception as exc:
                _log.error("run %s: storing run_finished failed: %s", run_id, _type_name(exc))
                return None
            state.seq = terminal.seq
            self._publish(run_id, state, terminal)
            return terminal
        return None

    def _artefacts(
        self, row: RunRow, state: _RunState, path: Path, outcome: RunOutcome
    ) -> tuple[str | None, list[dict[str, Any]]]:
        """Build and store the preview (only after ``loaded``); order the transforms."""
        out = outcome.output
        transforms: list[dict[str, Any]] = []
        if out is not None:
            transforms = [dict(t) for t in out.meta.get("transforms") or []]
            order = {node_id: i for i, node_id in enumerate(state.plan)}
            transforms.sort(
                key=lambda t: (order.get(t.get("node_id"), len(order)), t.get("attempt", 0))
            )
        key: str | None = None
        if state.loaded and out is not None:
            try:
                frame = build_preview(read_input(path), out, load_contract(row.table))
                buffer = io.BytesIO()
                frame.write_parquet(buffer)
                key = preview_key(row.id)
                self._blobs.put_bytes(key, buffer.getvalue())
            except Exception as exc:
                key = None
                _log.error("run %s: building the preview failed: %s", row.id, _type_name(exc))
        return key, transforms

    def _save_and_finish(
        self, run_id: str, state: _RunState, key: str | None, transforms: list[dict[str, Any]]
    ) -> None:
        try:
            self._store.save_artefacts(run_id, preview_key=key, transforms=transforms)
        except Exception as exc:
            _log.error("run %s: saving artefacts failed: %s", run_id, _type_name(exc))
        self._finish_once(run_id, state)  # a retry, only if finishing at run_finished failed

    def _finish_once(self, run_id: str, state: _RunState) -> None:
        """``store.finish`` the run with :attr:`_RunState.finished`, unless already done."""
        if state.finished_stored or state.finished is None:
            return
        state.finished_stored = self._finish(run_id, state.finished)

    def _finish(self, run_id: str, terminal: RunFinished) -> bool:
        """``store.finish``; ``True`` when the run is now finished (by this call or earlier)."""
        try:
            self._store.finish(run_id, terminal.status, terminal.cost_usd)
        except RunNotActive:
            return True  # already finished (e.g. by the cancelling coroutine)
        except Exception as exc:
            _log.error("run %s: finish failed: %s", run_id, _type_name(exc))
            return False
        return True
