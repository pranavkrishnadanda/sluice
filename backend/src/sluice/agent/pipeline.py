"""The agent pipeline: a hand-written state machine over one input file (spec section 7.3).

States, in order (each one emits the contract event named after it, see ``docs/api/contract.md``,
"Event order"):

1. **read** the file; ``run_started`` (with the row count).
2. **map** (``propose_mapping`` -> ``apply_mapping``): the model renames source columns the
   deterministic matcher left over. No event of its own; its renames are listed in
   ``rules_applied.actions``.
3. **rules pass 1** (``profile`` + ``apply_rules``); ``profiled``, ``rules_applied``. A file whose
   primary key cannot be found is quarantined whole here, exactly as the rules-only system does.
4. **plan** (``candidate_columns`` -> ``propose_plan``); ``plan_created``. No candidates means no
   model call and an empty plan.
5. **work**, nodes in dependency waves, the nodes of one wave concurrently (``asyncio.gather``).
   Per node, up to ``MAX_ATTEMPTS`` x: ``route_tier`` -> ``write_transform`` (unchecked draft)
   -> ``precheck_code``
   -> ``deps.sandbox`` (in a worker thread) -> ``guard_output`` -> keep only the rewritten
   cells pass 1 flagged, in every raw copy of a row (Ruling F2: parsed cells keep their raw
   text; duplicates stay identical, so dedupe is unchanged) -> an effect check (the
   rewrite must recover a flagged cell and break no parsed one) -> ``verify_node``. Each
   attempt emits ``node_started`` then ``node_finished`` or ``attempt_failed``; a failure's
   reason is fed to the next attempt as ``prior_error`` (the precheck's, guard's or verifier's
   own reason).
   A node whose dependency was not accepted never starts. ``depends_on`` only orders and
   gates: a dependent node still reads the mapped input frame (never its dependency's
   rewritten column), and every accepted column is applied together in rules pass 2.
   **Failure path** (contract): when a node's final attempt fails, no new node starts (nodes in
   flight finish), there is no ``verified``, and the whole input is quarantined
   (``file_quarantined``, empty output) with ``run_finished`` status ``quarantined``.
6. **rules pass 2** on the raw frame with every accepted column replaced; ``verified`` (contract
   checks on the final output) unless the run failed.
7. ``run_finished``, exactly once (``loaded`` belongs to the loader / ``sluice run``). Its
   ``tokens``/``costUsd`` are the full run totals (mapping, planning and verifier calls too).

Fallbacks: model and provider problems never raise. ``LLMUnavailable`` (every route failed) or
``LLMOutputError`` from mapping or planning skip that step: the frame stays as it was, so the
output is what the rules make of it, and ``meta["llm_error"]`` says what happened. After an
``LLMUnavailable`` no further model call is made in the run. In a node, ``LLMOutputError`` (bad
code, a failed precheck, or a bad verdict) is an ordinary failed attempt. A mapping or planning
fallback does not make the run ``failed``: the status follows the data (``succeeded`` /
``quarantined``) and the summary names the step that fell back. ``LLMUnavailable`` in a worker
or verifier call keeps a planned node from being accepted, so the run could not complete: no new
attempt or node starts, the node gets no further ``node_started``, and once the nodes in flight
finish the run ends with ``run_finished`` (``failed``, no ``verified``); the output is rules
pass 1 with ``meta["llm_error"]``. Precedence (Ruling C4c): when a node exhausts its attempts
and a sibling hits such an outage, the whole-file quarantine wins (status ``quarantined``:
nothing loads either way, and the quarantine is the more specific signal). ``failed``
otherwise means a cap stopped a planned node, the sandbox itself
was unavailable, or the run crashed.

Crashes: any other ``Exception`` (read error, Docker API error, Polars error, internal bug) ends
the run with exactly one ``run_finished`` (``failed``, the summary naming only the exception
type) and a whole-file quarantine output. Nodes of one wave run in an ``asyncio.TaskGroup``, so
when one raises, its siblings are cancelled and awaited first. A sandbox call already running in
its worker thread cannot be killed: the task stops waiting for it, and the thread ends when the
container does, bounded by the runner's own ``SANDBOX_TIMEOUT_S`` (``asyncio.run`` waits for it
on shutdown). ``BaseException`` (cancellation, interrupts) is never caught.

Caps (``sluice.agent.caps``): ``MAX_TOKENS_PER_FILE`` and ``MAX_WALL_S_PER_FILE`` are checked
before every new attempt (spend and time measured from the start of the run, with ``deps.clock``)
and stop new attempts only; accepted work is kept in the output, ``meta["cap"]`` names the cap,
and the run is ``failed`` (its plan was not completed) with no ``verified``. The token cap is
checked per attempt, so nodes running concurrently can each overshoot it by at most one attempt
in flight (a worker call plus a verifier call): spend <= ``MAX_TOKENS_PER_FILE`` + nodes in the
wave x one attempt's tokens. The wall-clock cap has the same per-attempt granularity.

Cost: O(rows x columns) per rules pass; each attempt costs one rules pass on the rewritten frame
plus a model call or two; the number of attempts is bounded by ``MAX_ATTEMPTS`` x plan size.
Memory: a few copies of the input frame (raw, mapped, rewritten), plus O(events).
"""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Final

import polars as pl

from sluice import tracing
from sluice.agent import plan as planning
from sluice.agent.caps import (
    MAX_ATTEMPTS,
    MAX_ERROR_CHARS,
    MAX_TOKENS_PER_FILE,
    MAX_WALL_S_PER_FILE,
)
from sluice.agent.events import (
    AttemptFailed,
    EventLog,
    EventModel,
    Issue,
    NodeFinished,
    NodeStarted,
    PlanCreated,
    Profiled,
    RulesApplied,
    RunFinished,
    RunStarted,
    Verified,
)
from sluice.agent.events import PlanNode as EventPlanNode
from sluice.agent.mapping import ColumnMapping, apply_mapping, propose_mapping
from sluice.agent.verifier import MAX_VERIFY_PAIRS, verify_node
from sluice.agent.worker import clip, guard_output, precheck_code, write_transform
from sluice.contracts.model import ColumnSpec, Contract
from sluice.contracts.validate import validate
from sluice.evals.types import CaseInput, SystemOutput, pk_text
from sluice.io import read_input
from sluice.llm.client import LLM, CallRecord, LLMError, LLMOutputError, LLMUnavailable, Tier
from sluice.rules.fixers import (
    RuleResult,
    UnkeyableInputError,
    apply_rules,
    cell_texts,
    flagged_raw_rows,
)
from sluice.rules.profile import Profile, contract_sources, profile
from sluice.sandbox.runner import SandboxError, SandboxResult

__all__ = ["MAX_TOKENS_PER_FILE", "MAX_WALL_S_PER_FILE", "PipelineDeps", "run_pipeline"]

MAX_LLM_ERROR_CHARS: Final = 300
"""``meta["llm_error"]`` is cut to this length (the client's messages carry no provider text)."""

MAX_ISSUE_DETAIL_CHARS: Final = 200

Sandbox = Callable[[str, pl.Series], SandboxResult]


class PipelineError(RuntimeError):
    """An internal invariant broke; the run finishes as ``failed`` (see :func:`run_pipeline`)."""


def transform_id(run_id: str, node_id: str, attempt: int) -> str:
    """The contract ``transformId`` of an attempt: derived, opaque and globally unique.

    ``t`` + the first 32 hex digits of SHA-256 over (run id, node id, attempt): the same attempt
    always gets the same id, ids of different runs differ (the run id is a random UUID), and the
    result fits ``[A-Za-z0-9_-]{1,64}`` whatever the node id's length. ``f"{node_id}-{attempt}"``
    was not used: it is neither unique across runs nor bounded to 64 characters.
    """
    digest = hashlib.sha256(f"{run_id}\0{node_id}\0{attempt}".encode()).hexdigest()
    return f"t{digest[:32]}"


@dataclass(frozen=True, slots=True)
class PipelineDeps:
    """What the pipeline needs from outside: models, the sandbox and a monotonic clock.

    Attributes:
        llm: The model facade. Create and close it inside the event loop that runs the pipeline.
        sandbox: Runs one transform on a String column (``sluice.sandbox.runner.run_transform``
            in production). It blocks, so it is called in a worker thread.
        clock: Seconds from a monotonic clock (``time.monotonic``); latency and the wall cap use it.
    """

    llm: LLM
    sandbox: Sandbox
    clock: Callable[[], float]


# --------------------------------------------------------------------------- run state


@dataclass(slots=True)
class _NodeOutcome:
    node: planning.PlanNode
    attempts: int = 0
    accepted: bool = False
    tier: Tier = "small"
    values: pl.Series | None = None
    transforms: list[dict[str, Any]] = field(default_factory=list)
    stop: str | None = None
    outage: bool = False
    """A provider outage (no model route) kept this planned node from being accepted."""
    transform_id: str | None = None


@dataclass(slots=True)
class _Run:
    """Mutable state of one run (one coroutine tree, one event loop)."""

    case: CaseInput
    deps: PipelineDeps
    log: EventLog
    start: float
    records_from: int
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    emitted: list[EventModel] = field(default_factory=list)
    sources: dict[str, str | None] = field(default_factory=dict)
    """Contract column -> the raw input column it is read from (``meta["mapping"]``)."""
    errors: list[str] = field(default_factory=list)
    sandbox_error: str | None = None
    finished: bool = False
    halted: str | None = None
    """Set when a node's final attempt failed: no new node starts and the file is quarantined."""
    unavailable: bool = False
    cap: str | None = None

    def emit[E: EventModel](self, event: E) -> E:
        stamped = self.log.emit(event)
        self.emitted.append(stamped)
        return stamped

    @property
    def records(self) -> list[CallRecord]:
        return self.deps.llm.records[self.records_from :]

    def tokens(self) -> int:
        return sum(r.input_tokens + r.output_tokens for r in self.records)

    def cost(self) -> float:
        return sum(r.cost_usd for r in self.records)

    def elapsed(self) -> float:
        return self.deps.clock() - self.start

    def llm_failed(self, step: str, exc: LLMError) -> None:
        self.errors.append(f"{step}: {type(exc).__name__}: {exc}"[:MAX_LLM_ERROR_CHARS])
        if isinstance(exc, LLMUnavailable):
            self.unavailable = True

    def cap_reached(self) -> bool:
        """Whether a new attempt may not start; records which cap stopped it."""
        if self.cap is None:
            if self.tokens() >= MAX_TOKENS_PER_FILE:
                self.cap = "tokens"
            elif self.elapsed() >= MAX_WALL_S_PER_FILE:
                self.cap = "wall"
        return self.cap is not None


def _spend(records: list[CallRecord]) -> tuple[int, float]:
    return sum(r.input_tokens + r.output_tokens for r in records), sum(r.cost_usd for r in records)


# --------------------------------------------------------------------------- entry point


async def run_pipeline(
    case: CaseInput, deps: PipelineDeps, log: EventLog, *, run_id: str | None = None
) -> SystemOutput:
    """Run the agent pipeline over ``case.path`` (see the module docstring for the states).

    ``run_id`` is the caller's run id (``[A-Za-z0-9_-]{1,64}``), used for ``run_started``,
    ``meta["run_id"]`` and every transform id, so lineage, events and the API agree; ``None``
    makes up a random one. Events go to ``log`` as they happen (give it an ``on_emit`` to relay
    them live) and are also returned in ``meta["events"]``.

    Never raises for an ``Exception``: model and provider problems fall back as described in
    the module docstring, and anything unexpected (a read error, a sandbox or Docker API error,
    a Polars error, an internal bug) ends the run with exactly one ``run_finished`` (status
    ``failed``, the summary naming only the exception type) and a whole-file quarantine output.
    ``BaseException`` (``asyncio.CancelledError``, ``KeyboardInterrupt``) is never caught.
    """
    run = _Run(
        case,
        deps,
        log,
        start=deps.clock(),
        records_from=len(deps.llm.records),
        run_id=run_id or uuid.uuid4().hex,
    )
    try:
        # Rate-limit back-off inside any model call must end within the file's wall cap. Requests
        # themselves are not cut off, so a file can overrun the cap by up to one request.
        with deps.llm.wall_budget(MAX_WALL_S_PER_FILE):
            return await _pipeline(run)
    except Exception as exc:  # not BaseException: cancellation and interrupts propagate
        return _crashed(run, exc)


async def _pipeline(run: _Run) -> SystemOutput:
    case, deps = run.case, run.deps
    contract = case.contract
    raw = read_input(case.path)
    run.emit(
        RunStarted(
            run_id=run.run_id,
            file_name=case.path.name,
            table=contract.name,  # type: ignore[arg-type]  # validated by the event model
            rows=raw.height,
        )
    )

    mapping: ColumnMapping | None = None
    frame = raw
    try:
        with tracing.step_span("mapping"):
            mapping, _ = await propose_mapping(raw, contract, deps.llm)
            frame = apply_mapping(raw, mapping)
    except (LLMUnavailable, LLMOutputError) as exc:
        run.llm_failed("mapping", exc)
    map_actions = _mapping_actions(mapping)

    with tracing.step_span("profile"):
        prof1 = profile(frame, contract)
    run.sources = raw_sources(contract, prof1, mapping)
    try:
        with tracing.step_span("rules_pass_1"):
            pass1 = apply_rules(frame, contract, prof1, case.reference)
    except UnkeyableInputError as exc:
        reason = str(exc)
        actions = [*map_actions, f"file quarantined: {reason}"]
        empty = RuleResult(pl.DataFrame(), set(), set(), [])
        run.emit(Profiled(issues=_issues(prof1, empty, {}, mapping)))
        run.emit(RulesApplied(actions=actions))
        return _quarantine_file(run, actions, None, [], mapping, reason)

    candidates = planning.candidate_columns(frame, pass1, contract)
    run.emit(Profiled(issues=_issues(prof1, pass1, candidates, mapping)))
    run.emit(RulesApplied(actions=map_actions + pass1.actions))

    plan: planning.Plan | None = None
    if candidates and run.unavailable:
        run.errors.append("planning: skipped, no model route is available")
    else:
        try:
            with tracing.step_span("plan"):
                plan, _ = await planning.propose_plan(candidates, contract, deps.llm)
        except (LLMUnavailable, LLMOutputError) as exc:
            run.llm_failed("planning", exc)
    if plan is None:
        return _finish(run, pass1, map_actions + pass1.actions, None, [], mapping)

    run.emit(
        PlanCreated(
            nodes=[
                EventPlanNode(
                    id=n.id,
                    title=n.issue,
                    columns=[n.column],
                    tier=planning.route_tier(n, 1),
                    depends_on=list(n.depends_on),
                )
                for n in plan.nodes
            ]
        )
    )
    outcomes = await _run_nodes(run, plan, frame, prof1, candidates, _hints(mapping))
    if run.halted is not None:
        actions = [*map_actions, *pass1.actions, f"file quarantined: {run.halted}"]
        return _quarantine_file(run, actions, plan, outcomes, mapping, run.halted)

    if any(o.outage for o in outcomes):
        # A provider outage kept a planned node from being accepted: the run could not complete.
        # The output is rules pass 1 (accepted work of other nodes is not applied).
        return _finish(run, pass1, map_actions + pass1.actions, plan, outcomes, mapping)

    final = pass1
    accepted = {o.node.column: o.values for o in outcomes if o.accepted and o.values is not None}
    if accepted:
        source = contract_sources(prof1)
        rewritten = frame.with_columns(
            [values.alias(source[column]) for column, values in accepted.items()]
        )
        with tracing.step_span("rules_pass_2"):
            final = apply_rules(rewritten, contract, profile(rewritten, contract), case.reference)
    actions = map_actions + final.actions
    return _finish(run, final, actions, plan, outcomes, mapping)


# --------------------------------------------------------------------------- nodes


async def _run_nodes(
    run: _Run,
    plan: planning.Plan,
    frame: pl.DataFrame,
    prof: Profile,
    candidates: dict[str, list[str]],
    hints: dict[str, str],
) -> list[_NodeOutcome]:
    """Run the plan in dependency waves; returns one outcome per node, in plan order.

    ``hints`` maps a contract column to its original input column name (before the mapping
    renamed it), which is what the worker is shown: unit hints such as ``_paise`` live there.

    A wave is every not-yet-run node whose dependencies have all finished; its nodes run
    concurrently. A node whose dependency was not accepted is skipped (0 attempts).
    ``depends_on`` is a gate and an order only, not a data flow: every node transforms its
    column of the same ``frame`` (the mapped input), never a dependency's output. Once a
    node's final attempt fails (``run.halted``) no later wave starts; nodes already in flight
    run to completion and their events are still emitted.
    """
    outcomes = {n.id: _NodeOutcome(n, tier=planning.route_tier(n, 1)) for n in plan.nodes}
    pending = list(plan.nodes)
    done: set[str] = set()
    source = contract_sources(prof)
    flagged = flagged_raw_rows(frame, run.case.contract, prof)
    while pending:
        if run.halted is not None:  # a final failure: no new node starts
            for n in pending:
                outcomes[n.id].stop = "not started: the run is halted by a failed node"
            break
        wave = [n for n in pending if set(n.depends_on) <= done]
        pending = [n for n in pending if n not in wave]
        runnable = []
        for n in wave:
            if all(outcomes[d].accepted for d in n.depends_on):
                runnable.append(n)
            else:
                outcomes[n.id].stop = "dependency not accepted"
        # A TaskGroup, not a bare gather: if one node raises, its siblings are cancelled and
        # awaited before the error leaves (see the module docstring on sandbox threads).
        async with asyncio.TaskGroup() as group:
            for n in runnable:
                group.create_task(
                    _run_node(
                        run,
                        outcomes[n.id],
                        frame,
                        source[n.column],
                        hints.get(n.column, source[n.column]),
                        candidates[n.column],
                        flagged.get(n.column, set()),
                    )
                )
        done.update(n.id for n in wave)
    return [outcomes[n.id] for n in plan.nodes]


async def _run_node(
    run: _Run,
    outcome: _NodeOutcome,
    frame: pl.DataFrame,
    source_name: str,
    shown_name: str,
    samples: list[str],
    flagged_before: set[int],
) -> None:
    """Attempt one node until it is accepted, runs out of attempts, or a cap/outage stops it.

    ``source_name`` is the column in ``frame``; ``shown_name`` is the input file's own name for
    it (with any unit hint), passed to the worker.
    """
    node = outcome.node
    contract = run.case.contract
    spec = contract.column(node.column)
    texts = cell_texts(frame[source_name])
    if texts is None:  # candidate_columns only offers columns it could read as text
        raise PipelineError(f"node {node.id}: column {node.column!r} cannot be read as text")
    values = pl.Series(source_name, texts, dtype=pl.String)
    prior_error: str | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        if run.unavailable:
            outcome.stop, outcome.outage = "no model route is available", True
            return
        if run.cap_reached():
            outcome.stop = f"{run.cap} cap reached"
            return
        tier = planning.route_tier(node, attempt)
        outcome.attempts, outcome.tier = attempt, tier
        started = run.deps.clock()
        run.emit(NodeStarted(node_id=node.id, attempt=attempt, max_attempts=MAX_ATTEMPTS))
        records: list[CallRecord] = []
        error, rewritten = await _traced_attempt(
            run, outcome, attempt, tier, spec, source_name, shown_name, samples, prior_error,
            values, frame, flagged_before, records,
        )  # fmt: skip
        tokens, cost = _spend(records)
        if error is None:
            if rewritten is None:
                raise PipelineError(f"node {node.id}: accepted attempt without output")
            outcome.accepted, outcome.values = True, rewritten
            outcome.transform_id = transform_id(run.run_id, node.id, attempt)
            run.emit(
                NodeFinished(
                    node_id=node.id,
                    attempt=attempt,
                    tier=tier,
                    tokens=tokens,
                    cost_usd=cost,
                    latency_ms=max(0, round((run.deps.clock() - started) * 1000)),  # Ruling E1
                    transform_id=outcome.transform_id,
                )
            )
            return
        run.emit(
            AttemptFailed(
                node_id=node.id,
                attempt=attempt,
                max_attempts=MAX_ATTEMPTS,
                error=error,
                tokens=tokens,
                cost_usd=cost,
            )
        )
        if outcome.stop is not None:  # an outage or a broken sandbox: retrying cannot help
            return
        prior_error = clip(error, MAX_ERROR_CHARS)
    outcome.stop = f"no accepted transform after {MAX_ATTEMPTS} attempts"
    if run.halted is None:  # the contract's failure path: hold the whole input back
        run.halted = f"node {node.id} ({node.column}): {outcome.stop}"


async def _traced_attempt(
    run: _Run, outcome: _NodeOutcome, attempt: int, tier: Tier, *rest: Any
) -> tuple[str | None, pl.Series | None]:
    """`_attempt` inside its ``sluice.node.attempt`` span."""
    attributes = {"sluice.node_id": outcome.node.id, "sluice.attempt": attempt, "sluice.tier": tier}
    with tracing.span("sluice.node.attempt", attributes) as current:
        error, rewritten = await _attempt(run, outcome, attempt, tier, *rest)
        current.set_attribute("sluice.outcome", "accepted" if error is None else "failed")
    return error, rewritten


async def _attempt(
    run: _Run,
    outcome: _NodeOutcome,
    attempt: int,
    tier: Tier,
    spec: ColumnSpec,
    source_name: str,
    shown_name: str,
    samples: list[str],
    prior_error: str | None,
    values: pl.Series,
    frame: pl.DataFrame,
    flagged_before: set[int],
    records: list[CallRecord],
) -> tuple[str | None, pl.Series | None]:
    """One attempt: ``(None, rewritten values)`` when accepted, else ``(why, None)``.

    ``records`` collects the attempt's billed calls; ``outcome.stop`` is set when retrying is
    pointless (no model route, or the sandbox itself is down).
    """
    node, llm = outcome.node, run.deps.llm
    try:
        code, record = await write_transform(
            node, spec, shown_name, samples, prior_error, tier, llm, checked=False
        )
    except LLMOutputError as exc:
        records.append(exc.record)
        return f"the worker's output did not match the transform schema: {exc}", None
    except LLMUnavailable as exc:
        run.llm_failed(f"worker {node.id}", exc)
        outcome.stop, outcome.outage = "no model route is available", True
        return f"LLMUnavailable: {exc}", None
    records.append(record)
    transform = {
        "transform_id": transform_id(run.run_id, node.id, attempt),
        "node_id": node.id,
        "attempt": attempt,
        "code": code.code,
        "model": record.model or tier,
        "passed": False,
    }
    outcome.transforms.append(transform)

    # Prechecked here, not in the output schema, so the exact reason reaches the next attempt.
    if (reason := precheck_code(code.code)) is not None:
        return f"precheck: {reason}", None
    try:
        result = await asyncio.to_thread(run.deps.sandbox, code.code, values)
    except SandboxError as exc:
        run.sandbox_error = type(exc).__name__
        outcome.stop = "the sandbox is unavailable"
        return f"sandbox unavailable ({type(exc).__name__})", None
    if not result.ok or result.values is None:
        return f"sandbox: {result.error or 'no output'}", None
    out = result.values
    if out.dtype == pl.Null:  # Ruling W2: an all-null output is a String column of nulls
        out = out.cast(pl.String)
    if (reason := guard_output(values, out)) is not None:
        return reason, None
    out = only_flagged(values, out.alias(source_name), flagged_before)  # Ruling F2

    effect = _effect(run.case, frame, source_name, spec, values, out, flagged_before)
    if effect.error is not None:
        return effect.error, None
    try:
        verdict, vrecord = await verify_node(node, spec, effect.pairs, effect.stats, llm)
    except LLMOutputError as exc:
        records.append(exc.record)
        return f"the verifier's answer failed validation: {exc}", None
    except LLMUnavailable as exc:
        run.llm_failed(f"verifier {node.id}", exc)
        outcome.stop, outcome.outage = "no model route is available", True
        return f"LLMUnavailable: {exc}", None
    records.append(vrecord)
    if not verdict.accept:
        return f"verifier rejected the rewrite: {verdict.reason}", None
    transform["passed"] = True
    return None, out


# --------------------------------------------------------------------------- effect check


def only_flagged(raw: pl.Series, out: pl.Series, flagged: set[int]) -> pl.Series:
    """``out`` in the ``flagged`` rows, ``raw`` everywhere else (Ruling F2), named as ``out``.

    ``flagged`` comes from :func:`sluice.rules.fixers.flagged_raw_rows`: every raw row whose own
    cell the rules flag, duplicates included, so all copies of a duplicate row get the same
    rewrite and stay exact duplicates for the rules' dedupe.

    A rewrite may only change the cells rules pass 1 flagged for its column: a cell the rules
    parsed keeps its raw text, so a transform cannot alter a value the rules already read
    (whatever the verifier sees). Every whole-column repair the drift ops need (paise, IST)
    flags every row, so nothing a valid rewrite needs is lost. O(rows).
    """
    mask = pl.Series([i in flagged for i in range(raw.len())], dtype=pl.Boolean)
    merged = pl.select(pl.when(mask).then(out).otherwise(raw)).to_series()
    return merged.alias(out.name)


@dataclass(frozen=True, slots=True)
class _Effect:
    error: str | None
    pairs: list[tuple[str, str | None]]
    stats: dict[str, float | int | None]


def _effect(
    case: CaseInput,
    frame: pl.DataFrame,
    source_name: str,
    spec: ColumnSpec,
    raw: pl.Series,
    out: pl.Series,
    flagged_before: set[int],
) -> _Effect:
    """What a rewrite does to the rules' parse of its column (one extra rules pass).

    Rejects a rewrite that recovers no flagged cell, or that makes the rules flag a cell they
    parsed before. Flags are compared per raw row (``flagged_raw_rows``, before dedupe), so a
    duplicate copy is never mistaken for a newly broken cell. Builds the verifier's evidence:
    raw -> rewritten pairs (flagged rows first,
    then other changed rows, at most ``MAX_VERIFY_PAIRS``) and before/after statistics.
    """
    contract = case.contract
    rewritten = frame.with_columns(out)
    prof = profile(rewritten, contract)
    after = apply_rules(rewritten, contract, prof, case.reference)
    flagged_after = flagged_raw_rows(rewritten, contract, prof).get(spec.name, set())
    recovered = len(flagged_before - flagged_after)
    broken = sorted(flagged_after - flagged_before)
    stats: dict[str, float | int | None] = {
        "rows": raw.len(),
        "flagged_before": len(flagged_before),
        "flagged_after": len(flagged_after),
        "recovered": recovered,
        "newly_flagged": len(broken),
        "changed": int((raw.ne_missing(out)).sum()),
    }
    if broken:
        error = (
            f"the rewrite made {len(broken)} cell(s) unparseable that the rules parsed before "
            f"(rows {broken[:5]}); leave cells you do not need to change as they are"
        )
        return _Effect(error, [], stats)
    if recovered == 0:
        error = (
            f"the rewrite recovered none of the {len(flagged_before)} flagged cell(s): the rules "
            "still cannot parse its output; follow the target format exactly"
        )
        return _Effect(error, [], stats)
    stats |= _numeric_stats(after.df[spec.name], "after")

    raw_list, out_list = raw.to_list(), out.to_list()
    changed = [i for i, (a, b) in enumerate(zip(raw_list, out_list, strict=True)) if a != b]
    order = sorted(flagged_before) + [i for i in changed if i not in flagged_before]
    pairs: list[tuple[str, str | None]] = []
    for i in order:
        if raw_list[i] is not None:
            pairs.append((raw_list[i], out_list[i]))
            if len(pairs) == MAX_VERIFY_PAIRS:
                break
    return _Effect(None, pairs, stats)


def _numeric_stats(series: pl.Series, label: str) -> dict[str, float | int | None]:
    """Non-null count and, for numeric columns, min / max / mean as floats."""
    stats: dict[str, float | int | None] = {f"non_null_{label}": series.len() - series.null_count()}
    if series.dtype.is_numeric() and series.null_count() < series.len():
        as_float = series.cast(pl.Float64)
        for name, value in (
            ("min", as_float.min()),
            ("max", as_float.max()),
            ("mean", as_float.mean()),
        ):
            stats[f"{name}_{label}"] = None if value is None else float(value)  # type: ignore[arg-type]
    return stats


def raw_sources(
    contract: Contract, prof: Profile, mapping: ColumnMapping | None
) -> dict[str, str | None]:
    """Contract column -> the raw input column it is read from, in contract order.

    ``prof`` profiles the mapped frame; a column the mapping renamed is traced back to its
    original name (``amt`` for ``o_totalprice``). ``None`` when no input column holds it.
    """
    renamed = {} if mapping is None else {t: s for s, t in mapping.pairs.items() if t is not None}
    matched = contract_sources(prof)
    return {
        c.name: renamed.get(matched[c.name], matched[c.name]) if c.name in matched else None
        for c in contract.columns
    }


def _hints(mapping: ColumnMapping | None) -> dict[str, str]:
    """Contract column -> the original input column name the mapping renamed to it."""
    if mapping is None:
        return {}
    return {target: source for source, target in mapping.pairs.items() if target is not None}


# --------------------------------------------------------------------------- reporting


def _mapping_actions(mapping: ColumnMapping | None) -> list[str]:
    if mapping is None:
        return []
    actions = [
        f"mapped {source!r} -> {target!r}"
        for source, target in mapping.pairs.items()
        if target is not None and source != target
    ]
    actions += [
        f"dropped unmapped column {source!r}"
        for source, target in mapping.pairs.items()
        if target is None
    ]
    return actions


def _issues(
    prof: Profile,
    result: RuleResult,
    candidates: dict[str, list[str]],
    mapping: ColumnMapping | None,
) -> list[Issue]:
    """Contract-shaped issues found by mapping and rules pass 1 (``profiled.issues``)."""
    issues: list[Issue] = []
    if mapping is not None:
        issues += [
            Issue(
                column=target,
                kind="rename",
                detail=f"source column {source!r}"[:MAX_ISSUE_DETAIL_CHARS],
            )
            for source, target in mapping.pairs.items()
            if target is not None and source != target
        ]
    issues += [
        Issue(column=c, kind="missing_column", detail="no input column holds it")
        for c in prof.missing
    ]
    issues += [
        Issue(column=c, kind="extra_column", detail="not in the contract") for c in prof.extra
    ]
    counts: dict[str, int] = {}
    for _, column in result.flags:
        counts[column] = counts.get(column, 0) + 1
    for column, n in counts.items():
        if column in prof.missing:
            continue
        kind = "format" if column in candidates else "destroyed"
        detail = f"{n} cell(s) the rules could not parse"
        issues.append(Issue(column=column, kind=kind, detail=detail[:MAX_ISSUE_DETAIL_CHARS]))
    return issues


def _quarantine_reasons(result: RuleResult, contract: Contract) -> dict[str, str]:
    """Why each quarantined row was held back. Rows only leave the frame on a foreign-key miss."""
    if not result.quarantined:
        return {}
    fks = [
        f"{column} not in {table}.{ref}" for column, (table, ref) in contract.foreign_keys.items()
    ]
    reason = "foreign_key: " + ("; or ".join(fks) if fks else "not in the reference keys")
    return {pk_text(pk): reason for pk in result.quarantined}


def _verified(run: _Run, result: RuleResult) -> None:
    """``verified``: contract checks on the final output (one per column, plus the table)."""
    contract = run.case.contract
    report = validate(contract, result.df, run.case.reference, result.flags)
    total = len(contract.columns) + 1
    failing = {v.column for v in report.violations}
    run.emit(Verified(checks_passed=total - len(failing), checks_total=total))


def _status_and_summary(
    run: _Run, result: RuleResult | None, outcomes: list[_NodeOutcome], quarantine: str | None
) -> tuple[str, str]:
    """``run_finished`` status and summary (contract "Statuses", Rulings C1 and C4).

    * ``quarantined`` when the whole input is held back (``quarantine``) or rows were;
    * ``failed`` only when the run could not complete: a cap stopped a planned node, the
      sandbox itself was unavailable, or a provider outage kept a planned node from being
      accepted (a crash is reported by :func:`_crashed`);
    * otherwise ``succeeded``. A model/provider fallback (a step handed over to the rules) does
      not change the status; the summary names the step and the error type.
    """
    accepted = sum(o.accepted for o in outcomes)
    parts = [f"repaired {accepted} of {len(outcomes)} planned column(s)"]
    if quarantine is not None:
        parts.append(f"file quarantined: {quarantine}")
    elif result is not None:
        parts.append(
            f"{len(result.flags)} cell(s) flagged, {len(result.quarantined)} row(s) quarantined"
        )
    if run.errors:
        parts.append("fell back to the rules for " + "; ".join(run.errors))
    if run.cap is not None:
        parts.append(f"stopped by the {run.cap} cap")
    if run.sandbox_error is not None:
        parts.append(f"the sandbox is unavailable ({run.sandbox_error})")
    if quarantine is not None:
        status = "quarantined"
    elif run.cap is not None or run.sandbox_error is not None or any(o.outage for o in outcomes):
        status = "failed"
    elif result is not None and result.quarantined:
        status = "quarantined"
    else:
        status = "succeeded"
    return status, "; ".join(parts)


def _finish(
    run: _Run,
    result: RuleResult,
    actions: list[str],
    plan: planning.Plan | None,
    outcomes: list[_NodeOutcome],
    mapping: ColumnMapping | None,
) -> SystemOutput:
    """The output of a run that was not quarantined whole: ``verified`` unless it failed."""
    status, summary = _status_and_summary(run, result, outcomes, quarantine=None)
    if status != "failed":
        _verified(run, result)
    _run_finished(run, status, summary)
    reasons = _quarantine_reasons(result, run.case.contract)
    return SystemOutput(
        df=result.df,
        flags=result.flags,
        quarantined=result.quarantined,
        file_quarantined=False,
        cost_usd=run.cost(),
        latency_s=run.elapsed(),
        meta=_meta(run, actions, plan, outcomes, mapping, reasons),
    )


def _quarantine_file(
    run: _Run,
    actions: list[str],
    plan: planning.Plan | None,
    outcomes: list[_NodeOutcome],
    mapping: ColumnMapping | None,
    reason: str,
) -> SystemOutput:
    """Hold the whole input back, as the rules-only system does: empty output, no flags.

    Used when the primary key cannot be found, and on the contract's failure path (a node's
    final attempt failed). No ``verified``; ``run_finished`` has status ``quarantined``.
    """
    status, summary = _status_and_summary(run, None, outcomes, quarantine=reason)
    _run_finished(run, status, summary)
    meta = _meta(run, actions, plan, outcomes, mapping, {})
    meta["file_quarantine_reason"] = reason
    return SystemOutput(
        df=pl.DataFrame(schema=run.case.contract.polars_schema()),
        flags=set(),
        quarantined=set(),
        file_quarantined=True,
        cost_usd=run.cost(),
        latency_s=run.elapsed(),
        meta=meta,
    )


def _exception_name(exc: BaseException) -> str:
    """The type name of ``exc``, or of the first leaf of an exception group (no message)."""
    while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        exc = exc.exceptions[0]
    return type(exc).__name__


def _crashed(run: _Run, exc: Exception) -> SystemOutput:
    """Finish a run that raised: one ``run_finished`` (``failed``), whole-file quarantine.

    The output is the empty contract frame with ``file_quarantined=True``: nothing from a run
    that crashed half-way is trusted (the rules-only system has no crash path of its own; an
    error there propagates). Only the exception's type name is reported: messages can carry
    host paths or provider text.
    """
    name = _exception_name(exc)
    if not run.emitted:  # the input could not even be read: start the run first
        run.emit(
            RunStarted(
                run_id=run.run_id,
                file_name=run.case.path.name,
                table=run.case.contract.name,  # type: ignore[arg-type]
                rows=None,
            )
        )
    if not run.finished:
        _run_finished(run, "failed", f"the run failed: {name}")
    meta = _meta(run, [f"run failed: {name}"], None, [], None, {})
    meta["crash"] = name
    return SystemOutput(
        df=pl.DataFrame(schema=run.case.contract.polars_schema()),
        flags=set(),
        quarantined=set(),
        file_quarantined=True,
        cost_usd=run.cost(),
        latency_s=run.elapsed(),
        meta=meta,
    )


def _run_finished(run: _Run, status: str, summary: str) -> None:
    run.finished = True
    run.emit(
        RunFinished(
            status=status,  # type: ignore[arg-type]  # one of the FinalStatus literals
            summary=summary,
            cost_usd=run.cost(),
            tokens=run.tokens(),
        )
    )


def _meta(
    run: _Run,
    actions: list[str],
    plan: planning.Plan | None,
    outcomes: list[_NodeOutcome],
    mapping: ColumnMapping | None,
    quarantine_reasons: dict[str, str],
) -> dict[str, Any]:
    """JSON-serialisable diagnostics, incl. the lineage shapes ``sluice.load.iceberg`` reads."""
    meta: dict[str, Any] = {
        "run_id": run.run_id,
        "actions": actions,
        "events": [e.to_dict() for e in run.emitted],
        "plan": None if plan is None else plan.model_dump(mode="json"),
        "nodes": [
            {
                "node_id": o.node.id,
                "column": o.node.column,
                "difficulty": o.node.difficulty,
                "attempts": o.attempts,
                "accepted": o.accepted,
                "tier": o.tier,
                "stop": o.stop,
                "outage": o.outage,
                "transform_id": o.transform_id,
            }
            for o in outcomes
        ],
        "plan_nodes": [
            {
                "node_id": o.node.id,
                "column_name": o.node.column,
                "tier": o.tier,
                "attempts": o.attempts,
                "accepted": o.accepted,
            }
            for o in outcomes
        ],
        "transforms": [t for o in outcomes for t in o.transforms],
        "records": [r.model_dump(mode="json") for r in run.records],
        "tokens": run.tokens(),
        "quarantine_reasons": quarantine_reasons,
        "mapping": run.sources or dict.fromkeys(c.name for c in run.case.contract.columns),
        "mapping_truncated": [] if mapping is None else list(mapping.truncated),
    }
    if run.errors:
        meta["llm_error"] = run.errors[0]
        meta["llm_errors"] = list(run.errors)
    if run.cap is not None:
        meta["cap"] = run.cap
    if run.sandbox_error is not None:
        meta["sandbox_error"] = run.sandbox_error
    return meta
