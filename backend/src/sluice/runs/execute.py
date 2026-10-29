"""The shared execution core: run a system over one file, relay its events live, load the output.

:func:`execute_run` is what ``sluice run`` (``sluice.cli.run_file``) and the API worker both
call. It owns the run's **public event log**: one :class:`~sluice.agent.events.EventLog` whose
``seq`` starts at 1 with no gaps, and every event goes to the caller's ``emit`` as soon as it is
stamped, in this order (``docs/api/contract.md``, "Event order"):

1. ``run_started`` (``rows`` counted by reading the file once up front; ``null`` if that fails);
2. the pipeline's own events, minus its ``run_started``/``run_finished`` (and any ``loaded``).
   A system that streams (the agent pipeline, through ``CaseInput.on_event``) is relayed
   **live**, renumbered into the public log as each event happens. A system that does not (the
   rules-only baseline) is replayed from ``meta["events"]`` when it returns, or gets a single
   ``rules_applied`` built from ``meta["actions"]``;
3. ``loaded``, only when the load wrote a main-table snapshot;
4. exactly one ``run_finished``.

One run id end to end: the caller's ``run_id`` is the public ``runId``, the pipeline's run id
(``meta["run_id"]``), the lineage ``run_id`` and the input of every transform id, so each
``node_finished.transformId`` is a persisted ``transforms.transform_id``.

Outcome (Rulings F1, L1, C1-C4c, unchanged from the plan-2 CLI):

* the idempotency key ``(table, file SHA-256, contract version)`` is looked up first; a file
  already loaded is reported ``succeeded`` ("file already loaded") without running the system;
* the pipeline's own final ``run_finished`` decides the status (else it is derived from the
  output, for the rules-only system). ``failed`` loads nothing and claims no key; any other
  status is handed to ``load_fn`` (which re-checks the key under its locks);
* an exception emits ``run_finished(failed)`` (the summary names only the exception type: messages
  can carry connection strings or host paths) and is re-raised.

Blocking: the system call and the load are synchronous (seconds to minutes); the API worker
runs this in a thread. Memory: the input frame (read once here for the row count, again by the
system), the output, and O(events).
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Literal

from sluice import tracing
from sluice.agent.events import (
    PIPELINE_EVENT_ADAPTER,
    EventLog,
    EventModel,
    Loaded,
    RulesApplied,
    RunFinished,
    RunStarted,
)
from sluice.contracts.model import Contract, load_contract
from sluice.drift.cases import load_reference
from sluice.evals.runner import SYSTEMS
from sluice.evals.types import CaseInput, SystemOutput
from sluice.io import read_input
from sluice.load.iceberg import LoadResult, find_load, load_output
from sluice.settings import Settings, get_settings

__all__ = [
    "RunFn",
    "RunOutcome",
    "execute_run",
    "file_sha256",
    "replayed",
    "resolve_run_fn",
]

_log = logging.getLogger(__name__)

HASH_CHUNK_BYTES: Final = 1 << 20
OWN_EVENTS: Final = frozenset({"run_started", "loaded", "run_finished"})
"""Event types :func:`execute_run` emits itself; a pipeline's own are never forwarded."""

RunFn = Callable[[CaseInput], SystemOutput]
LoadFn = Callable[..., LoadResult]
LoadedFn = Callable[..., LoadResult | None]
Emit = Callable[[EventModel], None]
OnTransform = Callable[[dict[str, Any]], None]
Status = Literal["succeeded", "quarantined", "failed"]


@dataclass(frozen=True, slots=True)
class RunOutcome:
    """How a run ended.

    Attributes:
        result: The load's result (``skipped=True`` for a file already loaded); ``None`` when
            the run ``failed`` and nothing was loaded.
        status: The final ``run_finished.status``.
        output: The system's output; ``None`` when the system did not run (already loaded).
        cost_usd: Model spend of the run (``run_finished.costUsd``).
        tokens: Model tokens of the run (``run_finished.tokens``).
    """

    result: LoadResult | None
    status: Status
    output: SystemOutput | None
    cost_usd: float
    tokens: int


def file_sha256(path: Path) -> str:
    """SHA-256 of ``path``'s bytes, read in 1 MiB chunks (memory O(1) in the file size)."""
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(HASH_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def resolve_run_fn(note: Callable[[str], Any], systems: Mapping[str, Any] | None = None) -> RunFn:
    """The agent system's ``run``, looked up at call time; the rules system until it exists.

    The agent keeps LiteLLM's response cache (``cache=True``): a repeat run of a file should
    cost nothing, unlike an eval trial (Ruling F3). ``systems`` defaults to the eval registry.
    """
    if systems is None:
        systems = SYSTEMS
    factory = systems.get("agent")
    if factory is None:
        note("note: the agent system is not registered yet; running the rules-only system")
        return systems["rules"]().run
    return factory(cache=True).run


def replayed(out: SystemOutput) -> tuple[list[EventModel], RunFinished | None]:
    """The pipeline's events to forward (:data:`OWN_EVENTS` dropped) and its ``run_finished``.

    An output without ``meta["events"]`` (the rules-only system) gets one ``rules_applied``
    listing ``meta["actions"]``.
    """
    raw = out.meta.get("events")
    if not raw:
        return [RulesApplied(actions=[str(a) for a in out.meta.get("actions", [])])], None
    events: list[EventModel] = []
    final: RunFinished | None = None
    for item in raw:
        event = (
            item if isinstance(item, EventModel) else PIPELINE_EVENT_ADAPTER.validate_python(item)
        )
        if isinstance(event, RunFinished):
            final = event
        elif event.type not in OWN_EVENTS:  # type: ignore[attr-defined]
            events.append(event)
    return events, final


def _output_status(out: SystemOutput, final: RunFinished | None) -> Status:
    """The run's status: the pipeline's own, else derived from the output (rules-only system)."""
    if final is not None:
        return final.status
    return "quarantined" if out.file_quarantined or out.quarantined else "succeeded"


def _skip_summary(result: LoadResult) -> str:
    return f"file already loaded (snapshot {result.snapshot_id}); nothing written"


def _count_rows(path: Path) -> int | None:
    """Rows in the input file, or ``None`` when it cannot be read (the run reports why later)."""
    try:
        return read_input(path).height
    except Exception:  # any read error: the row count is only informative
        return None


class _PublicLog:
    """The run's public event log: stamps each event and hands it to ``emit`` right away.

    Also keeps the spend seen so far, so a crash still reports it in ``run_finished``.
    """

    __slots__ = ("_emit", "_log", "cost", "relayed", "tokens")

    def __init__(self, emit: Emit) -> None:
        self._emit = emit
        self._log = EventLog()
        self.cost, self.tokens = 0.0, 0
        self.relayed = False
        """Set once the system streamed an event live (nothing to replay from its meta)."""

    def publish(self, event: EventModel) -> None:
        self._emit(self._log.emit(event))

    def relay(self, event: EventModel) -> None:
        """``CaseInput.on_event``: forward a pipeline event as it happens (renumbered)."""
        self.relayed = True
        if event.type not in OWN_EVENTS:  # type: ignore[attr-defined]
            self.publish(event)

    def finished(self, status: Status, summary: str) -> RunFinished:
        return RunFinished(status=status, summary=summary, cost_usd=self.cost, tokens=self.tokens)


@dataclass(frozen=True, slots=True)
class _Ending:
    outcome: RunOutcome
    loaded: Loaded | None
    finished: RunFinished


def execute_run(
    path: Path,
    table: str,
    *,
    run_id: str,
    emit: Emit,
    on_transform: OnTransform | None = None,
    contract_version: str = "v1",
    run_fn: RunFn | None = None,
    load_fn: LoadFn = load_output,
    loaded_fn: LoadedFn = find_load,
    settings_fn: Callable[[], Settings] = get_settings,
    reference_fn: Callable[[str], dict[str, set[Any]]] = load_reference,
) -> RunOutcome:
    """Run ``path`` into ``table`` and stream its events to ``emit`` (see the module docstring).

    Args:
        path: The input file (CSV, JSON or Parquet).
        table: The target contract table (``orders`` / ``lineitem``).
        run_id: The run's opaque id (``[A-Za-z0-9_-]{1,64}``), used end to end.
        emit: Receives each public event, stamped, as it happens (from the calling thread).
        on_transform: Passed to the system as ``CaseInput.on_transform`` (each transform
            record, before the event that announces it); ``None`` (``sluice run``): not used.
        contract_version: The contract version to load against.
        run_fn: The system to run (default: :func:`resolve_run_fn`, resolved only when needed).
        load_fn: Loads the output (``sluice.load.iceberg.load_output``).
        loaded_fn: Looks up the idempotency key (``sluice.load.iceberg.find_load``).
        settings_fn: The service settings, read once, after ``run_started``.
        reference_fn: Parent key sets for foreign-key checks, by table.

    Returns:
        The outcome; ``status == "failed"`` means nothing was loaded and no key was claimed.

    Raises:
        Exception: Whatever the system or the load raised, after ``run_finished(failed)``.
            An unknown table or contract version, or an invalid ``run_id``, raises before
            any event.
    """
    contract = load_contract(table, contract_version)
    log = _PublicLog(emit)
    # The trace root: the pipeline's step and node-attempt spans (and the load step) nest here.
    with tracing.span("sluice.run", {"sluice.run_id": run_id, "sluice.table": table}) as root:
        log.publish(
            RunStarted(run_id=run_id, file_name=path.name, table=table, rows=_count_rows(path))  # type: ignore[arg-type]
        )
        try:
            ending = _run_and_load(
                path, contract, run_id, log, run_fn, load_fn, loaded_fn, settings_fn,
                reference_fn, on_transform,
            )  # fmt: skip
        except Exception as exc:
            root.set_attribute("sluice.status", "failed")
            log.publish(log.finished("failed", type(exc).__name__))
            raise
        root.set_attribute("sluice.status", ending.outcome.status)
        if ending.loaded is not None:
            log.publish(ending.loaded)
        log.publish(ending.finished)
    return ending.outcome


def _run_and_load(
    path: Path,
    contract: Contract,
    run_id: str,
    log: _PublicLog,
    run_fn: RunFn | None,
    load_fn: LoadFn,
    loaded_fn: LoadedFn,
    settings_fn: Callable[[], Settings],
    reference_fn: Callable[[str], dict[str, set[Any]]],
    on_transform: OnTransform | None,
) -> _Ending:
    """Everything between ``run_started`` and the final events; raises on any failure."""
    sha = file_sha256(path)
    settings = settings_fn()
    prior = loaded_fn(contract, sha, settings=settings)
    if prior is not None:
        outcome = RunOutcome(result=prior, status="succeeded", output=None, cost_usd=0.0, tokens=0)
        return _Ending(outcome, None, log.finished("succeeded", _skip_summary(prior)))

    fn = run_fn or resolve_run_fn(note=_log.warning)
    case = CaseInput(
        path,
        contract,
        reference_fn(contract.name),
        run_id=run_id,
        on_event=log.relay,
        on_transform=on_transform,
    )
    out = fn(case)
    log.cost = max(out.cost_usd, 0.0)
    log.tokens = max(int(out.meta.get("tokens", 0)), 0)
    events, final = replayed(out)
    if not log.relayed:
        for event in events:
            log.publish(event)
    status = _output_status(out, final)
    if status == "failed":
        summary = final.summary if final is not None else "the run failed"
        outcome = RunOutcome(None, "failed", out, log.cost, log.tokens)
        return _Ending(outcome, None, log.finished("failed", summary))

    with tracing.step_span("load"):
        result = load_fn(
            out, contract, run_id=run_id, file_sha256=sha, settings=settings, status=status
        )
    loaded = None
    if not result.skipped and result.snapshot_id is not None:
        loaded = Loaded(
            rows_loaded=result.rows_loaded,
            rows_quarantined=result.rows_quarantined,
            snapshot_id=result.snapshot_id,
        )
    if result.skipped:
        summary = _skip_summary(result)
    elif out.file_quarantined:
        summary = "nothing loaded; the file can be retried"
    else:
        summary = f"loaded {result.rows_loaded} row(s), quarantined {result.rows_quarantined}" + (
            " (recovered an earlier commit)" if result.recovered else ""
        )
    if final is not None and not result.skipped:
        summary = f"{final.summary}; {summary}"
    final_status: Status = "succeeded" if result.skipped else status
    outcome = RunOutcome(result, final_status, out, log.cost, log.tokens)
    return _Ending(outcome, loaded, log.finished(final_status, summary))
