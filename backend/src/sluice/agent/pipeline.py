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
   -> ``deps.sandbox`` (in a worker thread) -> ``guard_output`` -> an effect check (the rewrite
   must recover a flagged cell and break no parsed one) -> ``verify_node``. Each attempt emits
   ``node_started`` then ``node_finished`` or ``attempt_failed``; a failure's reason is fed to
   the next attempt as ``prior_error`` (the precheck's, guard's or verifier's own reason).
   A node whose dependency was not accepted never starts.
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
code, a failed precheck, or a bad verdict) is an ordinary failed attempt, while
``LLMUnavailable`` stops that node and its column is left to the rules. A fallback does not make
the run ``failed``: the status follows the data (``succeeded`` / ``quarantined``) and the summary
names the step that fell back. ``failed`` means the run could not complete its plan: a cap
stopped a planned node, or the sandbox itself was unavailable.

Caps (``sluice.agent.caps``): ``MAX_TOKENS_PER_FILE`` and ``MAX_WALL_S_PER_FILE`` are checked
before every new attempt (spend and time measured from the start of the run, with ``deps.clock``)
and stop new attempts only; accepted work is kept in the output, ``meta["cap"]`` names the cap,
and the run is ``failed`` (its plan was not completed) with no ``verified``.

Cost: O(rows x columns) per rules pass; each attempt costs one rules pass on the rewritten frame
plus a model call or two; the number of attempts is bounded by ``MAX_ATTEMPTS`` x plan size.
Memory: a few copies of the input frame (raw, mapped, rewritten), plus O(events).
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Final

import polars as pl

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
from sluice.evals.types import PK, CaseInput, SystemOutput
from sluice.io import read_input
from sluice.llm.client import LLM, CallRecord, LLMError, LLMOutputError, LLMUnavailable, Tier
from sluice.rules.fixers import (
    RuleResult,
    UnkeyableInputError,
    apply_rules,
    cell_texts,
    kept_raw_rows,
)
from sluice.rules.profile import Profile, profile
from sluice.sandbox.runner import SandboxError, SandboxResult

__all__ = ["MAX_TOKENS_PER_FILE", "MAX_WALL_S_PER_FILE", "PipelineDeps", "run_pipeline"]

MAX_LLM_ERROR_CHARS: Final = 300
"""``meta["llm_error"]`` is cut to this length (the client's messages carry no provider text)."""

MAX_ISSUE_DETAIL_CHARS: Final = 200

Sandbox = Callable[[str, pl.Series], SandboxResult]


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
    errors: list[str] = field(default_factory=list)
    sandbox_error: str | None = None
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


async def run_pipeline(case: CaseInput, deps: PipelineDeps, log: EventLog) -> SystemOutput:
    """Run the agent pipeline over ``case.path`` (see the module docstring for the states).

    Never raises for model or provider problems; read errors and sandbox-infrastructure bugs
    other than :class:`~sluice.sandbox.runner.SandboxError` propagate.
    """
    run = _Run(case, deps, log, start=deps.clock(), records_from=len(deps.llm.records))
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
        mapping, _ = await propose_mapping(raw, contract, deps.llm)
        frame = apply_mapping(raw, mapping)
    except (LLMUnavailable, LLMOutputError) as exc:
        run.llm_failed("mapping", exc)
    map_actions = _mapping_actions(mapping)

    prof1 = profile(frame, contract)
    try:
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
    outcomes = await _run_nodes(run, plan, frame, prof1, pass1, candidates)
    if run.halted is not None:
        actions = [*map_actions, *pass1.actions, f"file quarantined: {run.halted}"]
        return _quarantine_file(run, actions, plan, outcomes, mapping, run.halted)

    final = pass1
    accepted = {o.node.column: o.values for o in outcomes if o.accepted and o.values is not None}
    if accepted:
        source = _sources(prof1)
        rewritten = frame.with_columns(
            [values.alias(source[column]) for column, values in accepted.items()]
        )
        final = apply_rules(rewritten, contract, profile(rewritten, contract), case.reference)
    actions = map_actions + final.actions
    return _finish(run, final, actions, plan, outcomes, mapping)


# --------------------------------------------------------------------------- nodes


async def _run_nodes(
    run: _Run,
    plan: planning.Plan,
    frame: pl.DataFrame,
    prof: Profile,
    pass1: RuleResult,
    candidates: dict[str, list[str]],
) -> list[_NodeOutcome]:
    """Run the plan in dependency waves; returns one outcome per node, in plan order.

    A wave is every not-yet-run node whose dependencies have all finished; its nodes run
    concurrently. A node whose dependency was not accepted is skipped (0 attempts). Once a
    node's final attempt fails (``run.halted``) no later wave starts; nodes already in flight
    run to completion and their events are still emitted.
    """
    outcomes = {n.id: _NodeOutcome(n, tier=planning.route_tier(n, 1)) for n in plan.nodes}
    pending = list(plan.nodes)
    done: set[str] = set()
    source = _sources(prof)
    flagged = _flagged_rows(frame, run.case.contract, prof, pass1)
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
        await asyncio.gather(
            *(
                _run_node(
                    run,
                    outcomes[n.id],
                    frame,
                    source[n.column],
                    candidates[n.column],
                    flagged.get(n.column, set()),
                )
                for n in runnable
            )
        )
        done.update(n.id for n in wave)
    return [outcomes[n.id] for n in plan.nodes]


async def _run_node(
    run: _Run,
    outcome: _NodeOutcome,
    frame: pl.DataFrame,
    source_name: str,
    samples: list[str],
    flagged_before: set[int],
) -> None:
    """Attempt one node until it is accepted, runs out of attempts, or a cap/outage stops it."""
    node = outcome.node
    contract = run.case.contract
    spec = contract.column(node.column)
    texts = cell_texts(frame[source_name])
    assert texts is not None  # candidate_columns only offers columns it could read as text
    values = pl.Series(source_name, texts, dtype=pl.String)
    prior_error: str | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        if run.unavailable:
            outcome.stop = "no model route is available"
            return
        if run.cap_reached():
            outcome.stop = f"{run.cap} cap reached"
            return
        tier = planning.route_tier(node, attempt)
        outcome.attempts, outcome.tier = attempt, tier
        started = run.deps.clock()
        run.emit(NodeStarted(node_id=node.id, attempt=attempt, max_attempts=MAX_ATTEMPTS))
        records: list[CallRecord] = []
        error, rewritten = await _attempt(
            run, outcome, attempt, tier, spec, source_name, samples, prior_error, values,
            frame, flagged_before, records,
        )  # fmt: skip
        tokens, cost = _spend(records)
        if error is None:
            assert rewritten is not None
            outcome.accepted, outcome.values = True, rewritten
            run.emit(
                NodeFinished(
                    node_id=node.id,
                    attempt=attempt,
                    tier=tier,
                    tokens=tokens,
                    cost_usd=cost,
                    latency_ms=max(0, round((run.deps.clock() - started) * 1000)),  # Ruling E1
                    transform_id=uuid.uuid4().hex,
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


async def _attempt(
    run: _Run,
    outcome: _NodeOutcome,
    attempt: int,
    tier: Tier,
    spec: ColumnSpec,
    source_name: str,
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
            node, spec, source_name, samples, prior_error, tier, llm, checked=False
        )
    except LLMOutputError as exc:
        records.append(exc.record)
        return f"the worker's output did not match the transform schema: {exc}", None
    except LLMUnavailable as exc:
        run.llm_failed(f"worker {node.id}", exc)
        outcome.stop = "no model route is available"
        return f"LLMUnavailable: {exc}", None
    records.append(record)
    transform = {
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
    out = out.alias(source_name)

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
        outcome.stop = "no model route is available"
        return f"LLMUnavailable: {exc}", None
    records.append(vrecord)
    if not verdict.accept:
        return f"verifier rejected the rewrite: {verdict.reason}", None
    transform["passed"] = True
    return None, out


# --------------------------------------------------------------------------- effect check


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
    parsed before. Builds the verifier's evidence: raw -> rewritten pairs (flagged rows first,
    then other changed rows, at most ``MAX_VERIFY_PAIRS``) and before/after statistics.
    """
    contract = case.contract
    rewritten = frame.with_columns(out)
    prof = profile(rewritten, contract)
    after = apply_rules(rewritten, contract, prof, case.reference)
    flagged_after = _flagged_rows(rewritten, contract, prof, after).get(spec.name, set())
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


def _flagged_rows(
    frame: pl.DataFrame, contract: Contract, prof: Profile, result: RuleResult
) -> dict[str, set[int]]:
    """Contract column -> raw ``frame`` row indices of its flagged cells (kept rows only)."""
    rows = kept_raw_rows(frame, contract, prof)
    out: dict[str, set[int]] = {}
    for key, column in result.flags:
        if key in rows:
            out.setdefault(column, set()).add(rows[key])
    return out


def _sources(prof: Profile) -> dict[str, str]:
    """Contract column -> the input column the rules matched to it."""
    return {
        col.matched_contract_column: name
        for name, col in prof.columns.items()
        if col.matched_contract_column is not None
    }


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
            Issue(column=target, kind="rename", detail=f"source column {source!r}"[:200])
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


def _pk_text(pk: PK) -> str:
    """The loader's key text (``sluice.load.iceberg.pk_text``): compact JSON of the key list."""
    return json.dumps(list(pk), separators=(",", ":"), ensure_ascii=False, default=str)


def _quarantine_reasons(result: RuleResult, contract: Contract) -> dict[str, str]:
    """Why each quarantined row was held back. Rows only leave the frame on a foreign-key miss."""
    if not result.quarantined:
        return {}
    fks = [
        f"{column} not in {table}.{ref}" for column, (table, ref) in contract.foreign_keys.items()
    ]
    reason = "foreign_key: " + ("; or ".join(fks) if fks else "not in the reference keys")
    return {_pk_text(pk): reason for pk in result.quarantined}


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
    * ``failed`` only when the run could not complete its plan: a cap stopped a planned node,
      or the sandbox itself was unavailable;
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
    elif run.cap is not None or run.sandbox_error is not None:
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


def _run_finished(run: _Run, status: str, summary: str) -> None:
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
