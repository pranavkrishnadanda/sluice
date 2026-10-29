"""The contract between a system under evaluation and the benchmark harness.

A system gets a :class:`CaseInput` (the drifted file, its contract and the parent-key sets for
foreign-key checks) and returns a :class:`SystemOutput`. Every primary-key tuple in ``flags`` and
``quarantined`` holds Python-native ``int``/``str`` values, exactly as the key appears in the
system's output (for quarantined rows: as it appeared in the input, after cleaning).
"""

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

import polars as pl

from sluice.contracts.model import Contract

if TYPE_CHECKING:
    from sluice.agent.events import EventModel

PK = tuple[Any, ...]


def pk_text(pk: PK) -> str:
    """A key tuple as compact JSON text (``(7, 3)`` -> ``[7,3]``); stable and unambiguous.

    The one key text shared by the pipeline (``meta["quarantine_reasons"]``) and the loader
    (the quarantine table's ``pk`` and the ``flags`` table).
    """
    return json.dumps(list(pk), separators=(",", ":"), ensure_ascii=False, default=str)


@dataclass(frozen=True, slots=True)
class CaseInput:
    """What a system is given for one case.

    Attributes:
        path: The drifted input file (CSV, JSON or Parquet).
        contract: The table contract the output must satisfy.
        reference: Parent key sets keyed ``"<table>.<column>"`` (``{}`` when there are no FKs).
        run_id: The caller's run id (``sluice.runs.execute``), used as the run's own id by a
            system that has one (the agent pipeline: events, ``meta["run_id"]``, transform
            ids). ``None`` (evals): the system makes one up.
        on_event: Live sink for the run's events, called with each event as it is emitted by a
            system that can stream them (the agent pipeline). Systems that cannot ignore it;
            their events (if any) are only in ``meta["events"]``.
        on_transform: Live sink for transform records (the ``meta["transforms"]`` shape, incl.
            ``transform_id``), called once per attempt that produced code, with ``passed``
            final, **before** that attempt's ``node_finished``/``attempt_failed`` event, by a
            system that has transforms (the agent pipeline). The API worker persists them so a
            ``node_finished.transformId`` is resolvable as soon as it is delivered.
        on_activity: Live sink for ``activity`` events (what the run is busy with now), called
            when each phase starts by a system that has phases (the agent pipeline). They are
            live-only status: never in ``meta["events"]`` and unnumbered (the caller stamps
            them into its own log). ``None`` (evals, ``sluice run``): none are built.
    """

    path: Path
    contract: Contract
    reference: dict[str, set[Any]]
    run_id: str | None = None
    on_event: Callable[[EventModel], None] | None = None
    on_transform: Callable[[dict[str, Any]], None] | None = None
    on_activity: Callable[[EventModel], None] | None = None


@dataclass(frozen=True, slots=True)
class SystemOutput:
    """What a system returns for one case.

    Attributes:
        df: Cleaned rows in the exact contract schema (column order and dtypes).
        flags: ``(pk, contract_column)`` cells the system could not recover; each is null in
            ``df``.
        quarantined: PKs of rows withheld from ``df`` (e.g. foreign-key misses).
        file_quarantined: True when the whole file was rejected; ``df`` is then empty.
        cost_usd: Model spend for the case (0 for systems without an LLM).
        latency_s: Wall-clock time spent in ``run``.
        meta: Free-form, JSON-serialisable diagnostics (e.g. the actions taken).
    """

    df: pl.DataFrame
    flags: set[tuple[PK, str]]
    quarantined: set[PK]
    file_quarantined: bool
    cost_usd: float
    latency_s: float
    meta: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class System(Protocol):
    """A pipeline under evaluation."""

    name: str

    def run(self, case: CaseInput) -> SystemOutput:
        """Ingest ``case.path`` and return contract-shaped output."""
        ...
