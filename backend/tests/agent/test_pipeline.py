"""Pipeline state machine: map -> rules -> plan -> work/sandbox/guard/verify -> rules, with caps.

No network and no Docker: a scripted Pydantic AI `FunctionModel` plays every role (dispatching on
the role's system prompt) and a fake sandbox maps known transform sources to canned Python
callables. Generated code is never executed here (Global Constraints).
"""

from __future__ import annotations

import asyncio
import json
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import polars as pl
import pytest
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    SystemPromptPart,
    ToolCallPart,
    UserPromptPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.usage import RequestUsage

from sluice import cli
from sluice.agent import pipeline
from sluice.agent.caps import MAX_ATTEMPTS
from sluice.agent.events import PIPELINE_EVENT_ADAPTER, EventLog, EventModel
from sluice.agent.mapping import SYSTEM_PROMPT as MAPPER_PROMPT
from sluice.agent.pipeline import PipelineDeps, run_pipeline
from sluice.agent.plan import SYSTEM_PROMPT as PLANNER_PROMPT
from sluice.agent.verifier import VERIFIER_SYSTEM_PROMPT
from sluice.agent.worker import WORKER_SYSTEM_PROMPT, precheck_code
from sluice.contracts.model import Contract, load_contract
from sluice.drift.cases import Case, Recipe, build_case
from sluice.evals.graders import grade
from sluice.evals.runner import SYSTEMS
from sluice.evals.types import CaseInput, SystemOutput
from sluice.llm.client import LLM
from sluice.load.iceberg import PlanNodeRecord, TransformRecord, pk_text
from sluice.sandbox.runner import SandboxResult
from sluice.systems.agent import AgentSystem
from sluice.systems.rules_only import RulesOnlySystem

TOKENS_PER_CALL = 150  # 100 in + 50 out, see Script.respond


def _module(body: str) -> str:
    return f"import polars as pl\n\n\ndef transform(values: pl.Series) -> pl.Series:\n{body}\n"


STRIP_COMMAS = _module(
    "    gone = values.str.strip_chars().is_in(['N/A', '', '-1', '###'])\n"
    "    return pl.select(pl.when(gone).then(None).otherwise(values.str.replace_all(',', '')))"
    ".to_series()"
)
CRASH = _module("    raise ValueError('boom')")
FILL_ZERO = _module("    return pl.Series(['0.00'] * len(values))")
ALL_NULL = _module("    return pl.Series([None] * len(values))")
NOOP = _module("    return values")
ISO_DATES = _module(
    "    return values.str.to_date('%d %b %Y', strict=False).dt.strftime('%Y-%m-%d')"
)
NO_TRANSFORM = "import polars as pl\n\nx = 1\n"  # fails precheck: no `def transform`
DOWN = "<provider outage>"  # a worker queue entry that makes that call fail with HTTP 503

_ROLE_BY_PROMPT = {
    MAPPER_PROMPT: "mapper",
    PLANNER_PROMPT: "orchestrator",
    WORKER_SYSTEM_PROMPT: "worker",
    VERIFIER_SYSTEM_PROMPT: "verifier",
}


def _iso_dates(values: pl.Series) -> pl.Series:
    parsed = values.str.to_date("%d %b %Y", strict=False).dt.strftime("%Y-%m-%d")
    return pl.Series(values.name, parsed).fill_null(values)


def _strip_commas(values: pl.Series) -> pl.Series:
    gone = values.str.strip_chars().is_in(["N/A", "", "-1", "###"])
    stripped = values.str.replace_all(",", "")
    return pl.select(pl.when(gone).then(None).otherwise(stripped)).to_series()


CANNED: dict[str, Callable[[pl.Series], SandboxResult]] = {
    STRIP_COMMAS: lambda v: SandboxResult(True, _strip_commas(v), None, 3),
    CRASH: lambda v: SandboxResult(False, None, "ValueError: boom (line 5)", 3),
    FILL_ZERO: lambda v: SandboxResult(True, pl.Series(["0.00"] * v.len()), None, 3),
    ALL_NULL: lambda v: SandboxResult(True, pl.Series([None] * v.len()), None, 3),
    NOOP: lambda v: SandboxResult(True, v, None, 3),
    ISO_DATES: lambda v: SandboxResult(True, _iso_dates(v), None, 3),
}


@dataclass
class FakeSandbox:
    """Maps a known transform source to a canned result; records each call."""

    on_call: Callable[[], None] | None = None
    calls: list[str] = field(default_factory=list)

    def __call__(self, code: str, values: pl.Series) -> SandboxResult:
        assert values.dtype == pl.String
        self.calls.append(code)
        if self.on_call is not None:
            self.on_call()
        return CANNED[code](values)


def _texts(messages: list[ModelMessage]) -> tuple[str, str]:
    system = user = ""
    for message in messages:
        if isinstance(message, ModelRequest):
            for part in message.parts:
                if isinstance(part, SystemPromptPart) and not system:
                    system = part.content
                elif isinstance(part, UserPromptPart) and not user:
                    user = str(part.content)
    return system, user


@dataclass
class Script:
    """A FunctionModel playing every role: mapper, orchestrator, worker (per column), verifier."""

    mapping: dict[str, Any] = field(default_factory=lambda: {"mappings": []})
    plan: dict[str, Any] = field(default_factory=lambda: {"nodes": []})
    workers: dict[str, list[str]] = field(default_factory=dict)
    verdicts: list[bool] = field(default_factory=list)
    down: frozenset[str] = frozenset()
    down_columns: frozenset[str] = frozenset()  # worker outage for these columns only
    calls: list[tuple[str, str]] = field(default_factory=list)

    def respond(self, messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        system, user = _texts(messages)
        role = _ROLE_BY_PROMPT[system]
        self.calls.append((role, user))
        if role in self.down:
            raise ModelHTTPError(503, "script")
        if role == "mapper":
            out: dict[str, Any] = self.mapping
        elif role == "orchestrator":
            out = self.plan
        elif role == "worker":
            match = re.search(r'column "([^"]+)"', user)
            assert match is not None
            if match.group(1) in self.down_columns:
                raise ModelHTTPError(503, "script")
            queue = self.workers[match.group(1)]
            code = queue.pop(0) if len(queue) > 1 else queue[0]
            if code == DOWN:
                raise ModelHTTPError(503, "script")
            out = {"code": code, "explanation": "fix"}
        else:
            accept = self.verdicts.pop(0) if self.verdicts else True
            out = {"accept": accept, "reason": "pairs look right" if accept else "100x too large"}
        return ModelResponse(
            parts=[ToolCallPart(info.output_tools[0].name, json.dumps(out))],
            usage=RequestUsage(input_tokens=100, output_tokens=50),
            model_name="script-model",
        )

    def roles(self, role: str) -> list[str]:
        return [user for r, user in self.calls if r == role]


def node(nid: str, column: str, *deps: str, difficulty: str = "easy") -> dict[str, Any]:
    return {
        "id": nid,
        "column": column,
        "issue": f"{column} needs a reformat",
        "difficulty": difficulty,
        "depends_on": list(deps),
    }


@dataclass
class Clock:
    now: float = 0.0

    def __call__(self) -> float:
        return self.now


@dataclass
class Result:
    out: SystemOutput
    log: EventLog
    llm: LLM

    def events(self, kind: str | None = None) -> list[EventModel]:
        return [e for e in self.log.events if kind is None or e.type == kind]  # type: ignore[attr-defined]


def run(
    case: Case,
    script: Script,
    *,
    sandbox: FakeSandbox | None = None,
    clock: Callable[[], float] = time.monotonic,
    contract: Contract | None = None,
    reference: dict[str, set[Any]] | None = None,
    path: Path | None = None,
) -> Result:
    async def go() -> Result:
        model = FunctionModel(script.respond, model_name="script-model")
        async with LLM("http://litellm.test", "k", model_override=model) as llm:
            log = EventLog()
            deps = PipelineDeps(llm=llm, sandbox=sandbox or FakeSandbox(), clock=clock)
            case_input = CaseInput(
                path or case.input_path, contract or load_contract(case.table), reference or {}
            )
            out = await run_pipeline(case_input, deps, log)
            return Result(out, log, llm)

    result = asyncio.run(go())
    check_event_order(result.log)
    return result


def rules_only(case: Case, reference: dict[str, set[Any]] | None = None) -> SystemOutput:
    return RulesOnlySystem().run(
        CaseInput(case.input_path, load_contract(case.table), reference or {})
    )


def assert_same_output(a: SystemOutput, b: SystemOutput) -> None:
    assert a.df.equals(b.df)
    assert a.flags == b.flags
    assert a.quarantined == b.quarantined
    assert a.file_quarantined == b.file_quarantined


_PHASE = {
    "run_started": 0,
    "profiled": 1,
    "rules_applied": 2,
    "plan_created": 3,
    "node_started": 4,
    "attempt_failed": 4,
    "node_finished": 4,
    "verified": 5,
    "loaded": 6,
    "run_finished": 7,
}


def check_event_order(log: EventLog) -> None:
    """The contract's event order (docs/api/contract.md, "Event order")."""
    events = log.events
    assert [e.seq for e in events] == list(range(1, len(events) + 1))
    types = [e.type for e in events]  # type: ignore[attr-defined]
    assert types[0] == "run_started"
    assert types[-1] == "run_finished"
    assert types.count("run_finished") == 1
    phases = [_PHASE[t] for t in types]
    assert phases == sorted(phases), types
    per_node: dict[str, list[EventModel]] = {}
    for e in events:
        if e.type in ("node_started", "attempt_failed", "node_finished"):  # type: ignore[attr-defined]
            per_node.setdefault(e.node_id, []).append(e)  # type: ignore[attr-defined]
    for seq in per_node.values():
        for i, e in enumerate(seq):
            if i % 2 == 0:
                assert e.type == "node_started" and e.attempt == i // 2 + 1  # type: ignore[attr-defined]
            else:
                assert e.type in ("attempt_failed", "node_finished")  # type: ignore[attr-defined]
                assert e.attempt == seq[i - 1].attempt  # type: ignore[attr-defined]
        assert all(e.type != "node_finished" for e in seq[:-1])  # type: ignore[attr-defined]
    if "verified" in types:
        assert not any(
            e.type == "attempt_failed" and e.attempt == e.max_attempts  # type: ignore[attr-defined]
            for e in events
        )


# --------------------------------------------------------------------------- cases


def _build(tmp: Path, orders_df: pl.DataFrame, case_id: str, ops: list[Any], n: int = 40) -> Case:
    recipe = Recipe("orders", "medium", ops, "csv", "utf-8", n)
    return build_case(case_id, "dev", recipe, {"orders": orders_df}, 7, tmp)


RENAME = ("rename", {"column": "o_totalprice", "new_name": "amt"})
INDIAN = ("indian_number_string", {"column": "o_totalprice"})
DESTROY = ("destroy", {"column": "o_totalprice", "frac": 0.1, "token": "N/A"})
DATES = ("date_format_mix", {"column": "o_orderdate", "frac": 0.5, "formats": ["%d %b %Y"]})
AMT_MAPPING = {"mappings": [{"source": "amt", "target": "o_totalprice", "reason": "price"}]}
PRICE_PLAN = {"nodes": [node("n1", "o_totalprice")]}


@pytest.fixture(scope="module")
def cases_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("pipeline-cases")


@pytest.fixture(scope="module")
def medium(cases_dir: Path, orders_df: pl.DataFrame) -> Case:
    """Renamed price column with Indian digit grouping and some destroyed cells."""
    return _build(cases_dir, orders_df, "dev-0001", [RENAME, INDIAN, DESTROY])


@pytest.fixture(scope="module")
def indian(cases_dir: Path, orders_df: pl.DataFrame) -> Case:
    """Indian digit grouping only: nothing for the mapper to do."""
    return _build(cases_dir, orders_df, "dev-0002", [INDIAN])


@pytest.fixture(scope="module")
def two_columns(cases_dir: Path, orders_df: pl.DataFrame) -> Case:
    return _build(cases_dir, orders_df, "dev-0003", [INDIAN, DATES])


@pytest.fixture(scope="module")
def clean(cases_dir: Path, orders_df: pl.DataFrame) -> Case:
    ops = [("whitespace", {"column": "o_orderpriority", "frac": 0.3})]
    return _build(cases_dir, orders_df, "dev-0004", ops)


def price_flags(out: SystemOutput) -> int:
    return sum(1 for _, col in out.flags if col == "o_totalprice")


# --------------------------------------------------------------------------- happy path


def test_pipeline_medium_case_beats_rules_without_fabrication(medium: Case) -> None:
    script = Script(mapping=AMT_MAPPING, plan=PRICE_PLAN, workers={"o_totalprice": [STRIP_COMMAS]})
    result = run(medium, script)

    agent_grade = grade(medium, result.out, {})
    rules_grade = grade(medium, rules_only(medium), {})
    assert agent_grade.cell_accuracy is not None and rules_grade.cell_accuracy is not None
    assert agent_grade.cell_accuracy > rules_grade.cell_accuracy
    assert agent_grade.fabrication_rate in (None, 0.0)
    assert agent_grade.cell_accuracy == 1.0


def test_pipeline_happy_path_emits_full_event_sequence(medium: Case) -> None:
    script = Script(mapping=AMT_MAPPING, plan=PRICE_PLAN, workers={"o_totalprice": [STRIP_COMMAS]})
    result = run(medium, script)
    types = [e.type for e in result.log.events]  # type: ignore[attr-defined]
    assert types == [
        "run_started",
        "profiled",
        "rules_applied",
        "plan_created",
        "node_started",
        "node_finished",
        "verified",
        "run_finished",
    ]
    (finished,) = result.events("run_finished")
    assert finished.status == "succeeded"  # type: ignore[attr-defined]
    assert finished.tokens == 4 * TOKENS_PER_CALL  # type: ignore[attr-defined]  # map, plan, work, verify
    node_tokens = sum(
        e.tokens for e in result.log.events if e.type in ("node_finished", "attempt_failed")
    )  # type: ignore[attr-defined]
    assert finished.tokens > node_tokens  # type: ignore[attr-defined]  # Ruling C2: full run totals
    (plan_created,) = result.events("plan_created")
    (pnode,) = plan_created.nodes  # type: ignore[attr-defined]
    assert (pnode.id, pnode.columns, pnode.tier) == ("n1", ["o_totalprice"], "small")
    (started,) = result.events("run_started")
    assert started.rows == 40 + 0  # type: ignore[attr-defined]
    (done,) = result.events("node_finished")
    assert isinstance(done.latency_ms, int)  # type: ignore[attr-defined]  # Ruling E1
    assert done.tokens == 2 * TOKENS_PER_CALL  # type: ignore[attr-defined]  # worker + verifier


def test_pipeline_cost_is_sum_of_records(medium: Case) -> None:
    script = Script(mapping=AMT_MAPPING, plan=PRICE_PLAN, workers={"o_totalprice": [STRIP_COMMAS]})
    result = run(medium, script)
    assert result.out.cost_usd == pytest.approx(sum(r.cost_usd for r in result.llm.records))
    assert result.out.cost_usd > 0
    assert result.out.meta["records"] == [r.model_dump() for r in result.llm.records]
    assert result.out.latency_s >= 0


# --------------------------------------------------------------------------- repair loop


def test_pipeline_failed_attempt_retried_with_escalation_small_to_strong(indian: Case) -> None:
    script = Script(plan=PRICE_PLAN, workers={"o_totalprice": [CRASH, STRIP_COMMAS]})
    result = run(indian, script)

    tiers = [r.tier for r in result.llm.records if r.role == "worker"]
    assert tiers == ["small", "strong"]
    assert "boom" in script.roles("worker")[1]  # the failure is fed back as prior_error
    (failed,) = result.events("attempt_failed")
    assert failed.attempt == 1 and "boom" in failed.error  # type: ignore[attr-defined]
    assert failed.tokens == TOKENS_PER_CALL  # type: ignore[attr-defined]
    (done,) = result.events("node_finished")
    assert (done.attempt, done.tier) == (2, "strong")  # type: ignore[attr-defined]
    assert price_flags(result.out) == 0
    transforms = result.out.meta["transforms"]
    assert [(t["attempt"], t["passed"]) for t in transforms] == [(1, False), (2, True)]
    (pn,) = result.out.meta["plan_nodes"]
    assert pn == {
        "node_id": "n1",
        "column_name": "o_totalprice",
        "tier": "strong",
        "attempts": 2,
        "accepted": True,
    }


def test_pipeline_stops_at_max_attempts_and_quarantines_the_whole_file(indian: Case) -> None:
    sandbox = FakeSandbox()
    result = run(
        indian, Script(plan=PRICE_PLAN, workers={"o_totalprice": [CRASH]}), sandbox=sandbox
    )

    assert len(sandbox.calls) == MAX_ATTEMPTS
    failed = result.events("attempt_failed")
    assert [e.attempt for e in failed] == [1, 2, 3]  # type: ignore[attr-defined]
    assert all(e.max_attempts == MAX_ATTEMPTS for e in failed)  # type: ignore[attr-defined]
    assert not result.events("node_finished")
    assert not result.events("verified")  # contract failure path
    (finished,) = result.events("run_finished")
    assert finished.status == "quarantined"  # type: ignore[attr-defined]
    assert "n1" in finished.summary  # type: ignore[attr-defined]
    assert result.out.file_quarantined  # Ruling C1: the input is held back
    assert (
        result.out.df.is_empty() and result.out.df.schema == load_contract("orders").polars_schema()
    )
    assert result.out.flags == set() and result.out.quarantined == set()
    assert "n1" in result.out.meta["file_quarantine_reason"]
    (pn,) = result.out.meta["plan_nodes"]
    assert (pn["attempts"], pn["accepted"]) == (MAX_ATTEMPTS, False)
    assert [t["passed"] for t in result.out.meta["transforms"]] == [False] * MAX_ATTEMPTS
    assert grade(indian, result.out, {}).fabrication_rate in (None, 0.0)


def test_pipeline_final_failure_starts_no_new_nodes_but_lets_in_flight_ones_finish(
    cases_dir: Path, orders_df: pl.DataFrame
) -> None:
    custkey = ("indian_number_string", {"column": "o_custkey"})
    case = _build(cases_dir, orders_df, "dev-0007", [INDIAN, DATES, custkey])
    plan = {
        "nodes": [
            node("n1", "o_totalprice"),
            node("n2", "o_orderdate"),
            node("n3", "o_custkey", "n2"),  # a later wave: must not start after n1 fails
        ]
    }
    workers = {
        "o_totalprice": [CRASH],
        "o_orderdate": [ISO_DATES],
        "o_custkey": [STRIP_COMMAS],
    }
    result = run(case, Script(plan=plan, workers=workers))
    started = {e.node_id for e in result.events("node_started")}  # type: ignore[attr-defined]
    assert started == {"n1", "n2"}
    assert [e.node_id for e in result.events("node_finished")] == ["n2"]  # type: ignore[attr-defined]
    n3 = result.out.meta["plan_nodes"][2]
    assert (n3["attempts"], n3["accepted"]) == (0, False)
    assert result.out.file_quarantined
    assert not result.events("verified")
    (finished,) = result.events("run_finished")
    assert finished.status == "quarantined"  # type: ignore[attr-defined]


def test_pipeline_fabricating_transform_is_rejected_before_rules(medium: Case) -> None:
    script = Script(
        mapping=AMT_MAPPING, plan=PRICE_PLAN, workers={"o_totalprice": [FILL_ZERO, STRIP_COMMAS]}
    )
    result = run(medium, script)
    (failed,) = result.events("attempt_failed")
    assert "fabrication" in failed.error  # type: ignore[attr-defined]
    assert "fabrication" in script.roles("worker")[1]  # the guard's reason is the prior_error
    assert len(script.roles("verifier")) == 1  # the guard stopped attempt 1 before the verifier
    assert grade(medium, result.out, {}).fabrication_rate in (None, 0.0)


def test_pipeline_verifier_rejection_is_a_failed_attempt(indian: Case) -> None:
    script = Script(plan=PRICE_PLAN, workers={"o_totalprice": [STRIP_COMMAS]}, verdicts=[False])
    result = run(indian, script)
    (failed,) = result.events("attempt_failed")
    assert "verifier" in failed.error and "100x" in failed.error  # type: ignore[attr-defined]
    assert "100x" in script.roles("worker")[1]
    assert failed.tokens == 2 * TOKENS_PER_CALL  # type: ignore[attr-defined]
    (done,) = result.events("node_finished")
    assert done.attempt == 2  # type: ignore[attr-defined]


def test_pipeline_precheck_failure_feeds_its_reason_to_the_next_attempt(indian: Case) -> None:
    sandbox = FakeSandbox()
    script = Script(plan=PRICE_PLAN, workers={"o_totalprice": [NO_TRANSFORM, STRIP_COMMAS]})
    result = run(indian, script, sandbox=sandbox)
    assert sandbox.calls == [STRIP_COMMAS]  # no sandbox run for the code that failed precheck
    reason = precheck_code(NO_TRANSFORM)
    assert reason is not None
    (failed,) = result.events("attempt_failed")
    assert failed.attempt == 1 and reason in failed.error  # type: ignore[attr-defined]
    assert failed.tokens == TOKENS_PER_CALL  # type: ignore[attr-defined]  # one call, no repair
    worker_prompts = script.roles("worker")
    assert len(worker_prompts) == 2
    assert json.dumps(reason)[1:-1] in worker_prompts[1]  # Ruling C3: attempt 2 sees the reason
    assert result.out.meta["transforms"][0]["code"] == NO_TRANSFORM
    assert "llm_error" not in result.out.meta  # a bad transform is a repair signal, not an outage
    (done,) = result.events("node_finished")
    assert done.attempt == 2  # type: ignore[attr-defined]


def test_pipeline_schema_invalid_worker_output_is_a_failed_attempt(indian: Case) -> None:
    sandbox = FakeSandbox()
    # Attempt 1: empty code fails the schema twice (client repair retry) -> LLMOutputError.
    workers = {"o_totalprice": ["", "", STRIP_COMMAS]}
    result = run(indian, Script(plan=PRICE_PLAN, workers=workers), sandbox=sandbox)
    assert sandbox.calls == [STRIP_COMMAS]
    (failed,) = result.events("attempt_failed")
    assert failed.attempt == 1  # type: ignore[attr-defined]
    assert failed.tokens == 2 * TOKENS_PER_CALL  # type: ignore[attr-defined]
    assert "llm_error" not in result.out.meta  # a bad transform is a repair signal, not an outage
    (done,) = result.events("node_finished")
    assert done.attempt == 2  # type: ignore[attr-defined]


def test_pipeline_explicit_precheck_rejection_skips_sandbox(
    indian: Case, monkeypatch: pytest.MonkeyPatch
) -> None:
    answers = iter(["code is not allowed", None])
    monkeypatch.setattr(pipeline, "precheck_code", lambda code: next(answers))
    sandbox = FakeSandbox()
    script = Script(plan=PRICE_PLAN, workers={"o_totalprice": [STRIP_COMMAS]})
    result = run(indian, script, sandbox=sandbox)
    assert sandbox.calls == [STRIP_COMMAS]
    (failed,) = result.events("attempt_failed")
    assert "not allowed" in failed.error  # type: ignore[attr-defined]
    assert "not allowed" in script.roles("worker")[1]


def test_pipeline_all_null_output_is_cast_to_string_and_rejected(indian: Case) -> None:
    script = Script(plan=PRICE_PLAN, workers={"o_totalprice": [ALL_NULL, STRIP_COMMAS]})
    result = run(indian, script)  # Ruling W2: a pl.Null output must not crash the guard
    (failed,) = result.events("attempt_failed")
    assert "unparseable that the rules parsed before" in failed.error  # type: ignore[attr-defined]
    assert price_flags(result.out) == 0


def test_pipeline_transform_that_recovers_nothing_is_rejected(indian: Case) -> None:
    # NOOP changes nothing: no flagged cell recovered.
    script = Script(plan=PRICE_PLAN, workers={"o_totalprice": [NOOP, STRIP_COMMAS]})
    result = run(indian, script)
    (failed,) = result.events("attempt_failed")
    assert "recovered none" in failed.error  # type: ignore[attr-defined]
    assert len(script.roles("verifier")) == 1


# --------------------------------------------------------------------------- DAG and concurrency


def test_pipeline_independent_nodes_run_concurrently(two_columns: Case) -> None:
    barrier = threading.Barrier(2, timeout=10)  # deadlocks (BrokenBarrierError) if sequential
    plan = {"nodes": [node("n1", "o_totalprice"), node("n2", "o_orderdate")]}
    script = Script(plan=plan, workers={"o_totalprice": [STRIP_COMMAS], "o_orderdate": [ISO_DATES]})
    result = run(two_columns, script, sandbox=FakeSandbox(on_call=barrier.wait))
    assert {e.node_id for e in result.events("node_finished")} == {"n1", "n2"}  # type: ignore[attr-defined]
    assert not result.out.flags
    plan_order = [n["node_id"] for n in result.out.meta["plan_nodes"]]
    assert plan_order == ["n1", "n2"]


def test_pipeline_dependent_node_is_skipped_when_its_dependency_fails(two_columns: Case) -> None:
    plan = {"nodes": [node("n1", "o_totalprice"), node("n2", "o_orderdate", "n1")]}
    script = Script(plan=plan, workers={"o_totalprice": [CRASH], "o_orderdate": [ISO_DATES]})
    result = run(two_columns, script)
    assert {e.node_id for e in result.events("node_started")} == {"n1"}  # type: ignore[attr-defined]
    n2 = result.out.meta["plan_nodes"][1]
    assert (n2["node_id"], n2["attempts"], n2["accepted"]) == ("n2", 0, False)
    assert result.out.file_quarantined


def test_pipeline_dependent_node_runs_after_its_dependency(two_columns: Case) -> None:
    plan = {"nodes": [node("n2", "o_orderdate", "n1"), node("n1", "o_totalprice")]}
    script = Script(plan=plan, workers={"o_totalprice": [STRIP_COMMAS], "o_orderdate": [ISO_DATES]})
    result = run(two_columns, script)
    started = [e.node_id for e in result.events("node_started")]  # type: ignore[attr-defined]
    assert started == ["n1", "n2"]
    assert not result.out.flags


# --------------------------------------------------------------------------- caps


def test_pipeline_token_cap_stops_new_attempts_but_keeps_accepted_work(
    two_columns: Case, monkeypatch: pytest.MonkeyPatch
) -> None:
    # plan + n1 (worker + verifier) = 3 calls; then the cap is reached and n2 never starts.
    monkeypatch.setattr(pipeline, "MAX_TOKENS_PER_FILE", 3 * TOKENS_PER_CALL)
    plan = {"nodes": [node("n1", "o_totalprice"), node("n2", "o_orderdate", "n1")]}
    script = Script(plan=plan, workers={"o_totalprice": [STRIP_COMMAS], "o_orderdate": [ISO_DATES]})
    result = run(two_columns, script)
    assert price_flags(result.out) == 0  # n1's accepted work is kept
    assert any(col == "o_orderdate" for _, col in result.out.flags)
    n1, n2 = result.out.meta["plan_nodes"]
    assert (n1["accepted"], n2["attempts"], n2["accepted"]) == (True, 0, False)
    assert result.out.meta["cap"] == "tokens"
    assert not result.events("verified")
    (finished,) = result.events("run_finished")
    assert finished.status == "failed" and "cap" in finished.summary  # type: ignore[attr-defined]


def test_pipeline_wall_clock_cap_stops_new_attempts(indian: Case) -> None:
    clock = Clock()

    def slow() -> None:
        clock.now += pipeline.MAX_WALL_S_PER_FILE + 1

    sandbox = FakeSandbox(on_call=slow)
    script = Script(plan=PRICE_PLAN, workers={"o_totalprice": [CRASH, STRIP_COMMAS]})
    result = run(indian, script, sandbox=sandbox, clock=clock)
    assert len(sandbox.calls) == 1
    assert len(result.events("node_started")) == 1
    assert result.out.meta["cap"] == "wall"
    assert result.out.latency_s == pytest.approx(pipeline.MAX_WALL_S_PER_FILE + 1)


# --------------------------------------------------------------------------- fallbacks


def test_pipeline_planner_unavailable_equals_rules_only(indian: Case) -> None:
    script = Script(down=frozenset({"orchestrator"}))
    result = run(indian, script)
    assert_same_output(result.out, rules_only(indian))
    assert "LLMUnavailable" in result.out.meta["llm_error"]
    assert "planning" in result.out.meta["llm_error"]
    assert not result.events("plan_created")
    assert result.events("verified")
    (finished,) = result.events("run_finished")
    assert finished.status == "succeeded"  # type: ignore[attr-defined]  # Ruling C4
    assert "planning" in finished.summary and "LLMUnavailable" in finished.summary  # type: ignore[attr-defined]


def test_pipeline_planner_invalid_output_falls_back_to_rules(indian: Case) -> None:
    bad = {"nodes": [node("n1", "o_comment")]}  # not a candidate, rejected twice
    result = run(indian, Script(plan=bad))
    assert_same_output(result.out, rules_only(indian))
    assert "LLMOutputError" in result.out.meta["llm_error"]
    (finished,) = result.events("run_finished")
    assert finished.status == "succeeded"  # type: ignore[attr-defined]
    assert "LLMOutputError" in finished.summary  # type: ignore[attr-defined]


def test_pipeline_provider_down_everywhere_equals_rules_only(medium: Case) -> None:
    roles = frozenset({"mapper", "orchestrator", "worker", "verifier"})
    script = Script(down=roles)
    result = run(medium, script)
    assert_same_output(result.out, rules_only(medium))
    assert "mapping" in result.out.meta["llm_error"]
    assert [r for r, _ in script.calls] == ["mapper"]  # no more calls once every route failed
    assert result.out.cost_usd == 0.0
    (finished,) = result.events("run_finished")
    assert finished.status == "succeeded"  # type: ignore[attr-defined]
    assert "mapping" in finished.summary  # type: ignore[attr-defined]


def test_pipeline_worker_unavailable_stops_node_and_records_error(indian: Case) -> None:
    script = Script(plan=PRICE_PLAN, down=frozenset({"worker"}))
    result = run(indian, script)
    assert len(script.roles("worker")) == 1
    (failed,) = result.events("attempt_failed")
    assert "LLMUnavailable" in failed.error  # type: ignore[attr-defined]
    assert "worker" in result.out.meta["llm_error"]
    assert_same_output(result.out, rules_only(indian))
    (pn,) = result.out.meta["plan_nodes"]
    assert (pn["attempts"], pn["accepted"]) == (1, False)
    assert result.out.meta["transforms"] == []  # no code was produced
    assert not result.out.file_quarantined
    # Ruling C4 (refined): the run could not complete -> failed, no verified, and the node's
    # last attempt_failed is followed directly by run_finished.
    types = [e.type for e in result.log.events]  # type: ignore[attr-defined]
    assert types[-2:] == ["attempt_failed", "run_finished"]
    (finished,) = result.events("run_finished")
    assert finished.status == "failed"  # type: ignore[attr-defined]
    assert "worker" in finished.summary and "LLMUnavailable" in finished.summary  # type: ignore[attr-defined]


def test_pipeline_verifier_outage_fails_the_run_with_rules_output(indian: Case) -> None:
    script = Script(
        plan=PRICE_PLAN, workers={"o_totalprice": [STRIP_COMMAS]}, down=frozenset({"verifier"})
    )
    result = run(indian, script)
    assert_same_output(result.out, rules_only(indian))
    assert "verifier" in result.out.meta["llm_error"]
    assert not result.events("verified")
    (finished,) = result.events("run_finished")
    assert finished.status == "failed"  # type: ignore[attr-defined]


def test_pipeline_worker_outage_discards_other_accepted_work(two_columns: Case) -> None:
    plan = {"nodes": [node("n1", "o_totalprice"), node("n2", "o_orderdate", "n1")]}
    script = Script(
        plan=plan,
        workers={"o_totalprice": [STRIP_COMMAS], "o_orderdate": [ISO_DATES]},
        down_columns=frozenset({"o_orderdate"}),
    )
    result = run(two_columns, script)
    n1, n2 = result.out.meta["plan_nodes"]
    assert (n1["accepted"], n2["accepted"]) == (True, False)
    assert_same_output(result.out, rules_only(two_columns))  # output = rules pass 1
    (finished,) = result.events("run_finished")
    assert finished.status == "failed"  # type: ignore[attr-defined]


def test_pipeline_planning_outage_after_mapping_falls_back_and_succeeds(medium: Case) -> None:
    # Mapping works, then every route is down: planning falls back (a fallback, not a failure).
    script = Script(mapping=AMT_MAPPING, down=frozenset({"orchestrator"}))
    result = run(medium, script)
    (finished,) = result.events("run_finished")
    assert finished.status == "succeeded"  # type: ignore[attr-defined]
    assert price_flags(result.out) > 0


def test_pipeline_no_candidates_makes_no_plan_call(clean: Case) -> None:
    script = Script()
    result = run(clean, script)
    assert script.calls == []
    (plan_created,) = result.events("plan_created")
    assert plan_created.nodes == []  # type: ignore[attr-defined]
    assert result.events("verified")
    (finished,) = result.events("run_finished")
    assert finished.status == "succeeded"  # type: ignore[attr-defined]
    assert_same_output(result.out, rules_only(clean))


def test_pipeline_unkeyable_file_is_quarantined_like_rules_only(
    cases_dir: Path, orders_df: pl.DataFrame
) -> None:
    case = _build(
        cases_dir, orders_df, "dev-0005", [("rename", {"column": "o_orderkey", "new_name": "zz"})]
    )
    mapping = {"mappings": [{"source": "zz", "target": None, "reason": "unsure"}]}
    result = run(case, Script(mapping=mapping))
    assert result.out.file_quarantined
    assert_same_output(result.out, rules_only(case))
    (finished,) = result.events("run_finished")
    assert finished.status == "quarantined"  # type: ignore[attr-defined]
    assert not result.events("plan_created")


# --------------------------------------------------------------------------- meta for Task 9


def test_pipeline_meta_matches_loader_records_and_is_json(medium: Case) -> None:
    script = Script(
        mapping=AMT_MAPPING, plan=PRICE_PLAN, workers={"o_totalprice": [CRASH, STRIP_COMMAS]}
    )
    result = run(medium, script)
    meta = result.out.meta
    json.dumps(meta)  # JSON-serialisable

    nodes = [PlanNodeRecord.model_validate(n) for n in meta["plan_nodes"]]
    transforms = [TransformRecord.model_validate(t) for t in meta["transforms"]]
    assert [n.node_id for n in nodes] == ["n1"]
    assert [(t.attempt, t.passed, t.model) for t in transforms] == [
        (1, False, "script-model"),
        (2, True, "script-model"),
    ]
    assert transforms[1].code == STRIP_COMMAS

    replayed = [PIPELINE_EVENT_ADAPTER.validate_python(e) for e in meta["events"]]
    assert replayed == list(result.log.events)  # Ruling E1: exactly the emitted events
    assert meta["mapping_truncated"] == []
    assert meta["quarantine_reasons"] == {}
    assert meta["tokens"] == sum(r.input_tokens + r.output_tokens for r in result.llm.records)
    assert meta["plan"] == PRICE_PLAN
    assert meta["actions"]


def test_pipeline_quarantine_reasons_are_keyed_by_loader_pk_text(
    cases_dir: Path, lineitem_df: pl.DataFrame, orders_df: pl.DataFrame
) -> None:
    recipe = Recipe(
        "lineitem",
        "medium",
        [("fk_break", {"column": "l_orderkey", "frac": 0.2})],
        "csv",
        "utf-8",
        30,
    )
    case = build_case(
        "dev-0006", "dev", recipe, {"lineitem": lineitem_df, "orders": orders_df}, 7, cases_dir
    )
    reference = {"orders.o_orderkey": set(orders_df["o_orderkey"].to_list())}
    result = run(case, Script(), reference=reference)
    assert result.out.quarantined
    reasons = result.out.meta["quarantine_reasons"]
    assert set(reasons) == {pk_text(pk) for pk in result.out.quarantined}
    assert all("orders.o_orderkey" in r for r in reasons.values())
    assert_same_output(result.out, rules_only(case, reference))
    (finished,) = result.events("run_finished")
    assert finished.status == "quarantined"  # type: ignore[attr-defined]


def test_cli_replay_accepts_pipeline_events(medium: Case) -> None:
    script = Script(mapping=AMT_MAPPING, plan=PRICE_PLAN, workers={"o_totalprice": [STRIP_COMMAS]})
    result = run(medium, script)
    replayed = cli._replayed(result.out)
    assert [e.type for e in replayed] == [  # type: ignore[attr-defined]
        "profiled",
        "rules_applied",
        "plan_created",
        "node_started",
        "node_finished",
        "verified",
    ]


# --------------------------------------------------------------------------- AgentSystem


def test_agent_system_is_registered_and_picked_by_cli() -> None:
    assert SYSTEMS["agent"] is AgentSystem
    assert AgentSystem.name == "agent"
    notes: list[str] = []
    fn = cli.resolve_run_fn(note=notes.append)
    assert notes == []
    assert isinstance(fn.__self__, AgentSystem)  # type: ignore[attr-defined]


def test_agent_system_runs_pipeline_in_its_own_event_loop(indian: Case) -> None:
    script = Script(plan=PRICE_PLAN, workers={"o_totalprice": [STRIP_COMMAS]})
    created: list[LLM] = []

    def factory() -> LLM:
        llm = LLM("http://litellm.test", "k", model_override=FunctionModel(script.respond))
        created.append(llm)
        return llm

    system = AgentSystem(llm_factory=factory, sandbox=FakeSandbox())
    first = system.run(CaseInput(indian.input_path, load_contract("orders"), {}))
    second = system.run(CaseInput(indian.input_path, load_contract("orders"), {}))
    assert len(created) == 2 and created[0] is not created[1]  # one LLM per asyncio.run
    assert price_flags(first) == 0
    assert_same_output(first, second)


# --------------------------------------------------------------------------- crashes (review 1)


@dataclass
class HostileSandbox:
    """n1's code raises a host-side error once n2's sandbox call is in flight (and blocked)."""

    entered: threading.Event = field(default_factory=threading.Event)
    release: threading.Event = field(default_factory=threading.Event)
    finished: list[str] = field(default_factory=list)

    def __call__(self, code: str, values: pl.Series) -> SandboxResult:
        if code == CRASH:
            assert self.entered.wait(10)
            raise RuntimeError("docker API exploded: secret-host-detail")
        self.entered.set()
        self.release.wait(2)  # bounded: asyncio.run waits for executor threads on shutdown
        self.finished.append(code)
        return CANNED[code](values)


def test_pipeline_unexpected_error_finishes_the_run_exactly_once(two_columns: Case) -> None:
    plan = {"nodes": [node("n1", "o_totalprice"), node("n2", "o_orderdate")]}
    script = Script(plan=plan, workers={"o_totalprice": [CRASH], "o_orderdate": [ISO_DATES]})
    sandbox = HostileSandbox()
    result = run(two_columns, script, sandbox=sandbox)  # type: ignore[arg-type]
    (finished,) = result.events("run_finished")
    assert finished.status == "failed"  # type: ignore[attr-defined]
    assert "RuntimeError" in finished.summary  # type: ignore[attr-defined]
    assert "secret-host-detail" not in finished.summary  # type: ignore[attr-defined]
    assert result.out.file_quarantined and result.out.df.is_empty()
    assert result.out.meta["crash"] == "RuntimeError"
    assert "secret-host-detail" not in json.dumps(result.out.meta)
    # The sibling n2 was cancelled: it started but never finished or failed an attempt.
    n2_events = [e.type for e in result.log.events if getattr(e, "node_id", None) == "n2"]
    assert n2_events == ["node_started"]


def test_pipeline_read_failure_still_emits_one_run_finished(tmp_path: Path) -> None:
    missing = tmp_path / "gone.csv"
    case = Case("dev-0099", "dev", "orders", tmp_path, {"format": "csv"})
    result = run(case, Script(), contract=load_contract("orders"), path=missing)
    types = [e.type for e in result.log.events]  # type: ignore[attr-defined]
    assert types == ["run_started", "run_finished"]
    (started,) = result.events("run_started")
    assert started.rows is None  # type: ignore[attr-defined]
    (finished,) = result.events("run_finished")
    assert finished.status == "failed" and "FileNotFoundError" in finished.summary  # type: ignore[attr-defined]


class Abort(BaseException):
    """Stands in for cancellation / interrupts: a BaseException the pipeline must not catch."""


def test_pipeline_base_exceptions_are_not_swallowed(indian: Case) -> None:
    calls = 0

    def clock() -> float:  # raises in the event-loop thread, on the first check after the start
        nonlocal calls
        calls += 1
        if calls > 1:
            raise Abort
        return 0.0

    script = Script(plan=PRICE_PLAN, workers={"o_totalprice": [STRIP_COMMAS]})
    with pytest.raises(BaseException) as excinfo:
        run(indian, script, clock=clock)
    assert not isinstance(excinfo.value, Exception)
    leaves = excinfo.value.exceptions if isinstance(excinfo.value, BaseExceptionGroup) else ()
    assert isinstance(excinfo.value, Abort) or all(isinstance(e, Abort) for e in leaves)


# --------------------------------------------------------------------------- cap overshoot


def test_pipeline_concurrent_nodes_overshoot_token_cap_by_at_most_one_attempt_each(
    two_columns: Case, monkeypatch: pytest.MonkeyPatch
) -> None:
    cap = TOKENS_PER_CALL + 1  # just above the planning call: both nodes may start
    monkeypatch.setattr(pipeline, "MAX_TOKENS_PER_FILE", cap)
    plan = {"nodes": [node("n1", "o_totalprice"), node("n2", "o_orderdate")]}
    script = Script(plan=plan, workers={"o_totalprice": [CRASH], "o_orderdate": [CRASH]})
    result = run(two_columns, script)
    one_attempt = 2 * TOKENS_PER_CALL  # worker + verifier
    assert result.out.meta["cap"] == "tokens"
    assert result.out.meta["tokens"] <= cap + len(plan["nodes"]) * one_attempt
    assert len(result.events("node_started")) <= len(plan["nodes"])


# --------------------------------------------------------------------------- transform ids


def test_pipeline_transform_id_is_derived_from_run_node_and_attempt(indian: Case) -> None:
    long_id = "n" * 64
    plan = {"nodes": [node(long_id, "o_totalprice")]}
    script = Script(plan=plan, workers={"o_totalprice": [CRASH, STRIP_COMMAS]})
    result = run(indian, script)
    (done,) = result.events("node_finished")
    expected = pipeline.transform_id(result.out.meta["run_id"], long_id, 2)
    assert done.transform_id == expected  # type: ignore[attr-defined]
    assert len(expected) <= 64
    assert pipeline.transform_id("other-run", long_id, 2) != expected  # globally unique
    (meta_node,) = result.out.meta["nodes"]
    assert meta_node["transform_id"] == expected


def test_pipeline_exhausted_node_quarantine_wins_over_sibling_outage(two_columns: Case) -> None:
    # Ruling C4c: n1 exhausts its attempts while sibling n2 hits a worker outage on attempt 3.
    plan = {"nodes": [node("n1", "o_totalprice"), node("n2", "o_orderdate")]}
    workers = {"o_totalprice": [CRASH], "o_orderdate": [CRASH, CRASH, DOWN]}
    result = run(two_columns, Script(plan=plan, workers=workers))
    n1, n2 = result.out.meta["plan_nodes"]
    assert (n1["attempts"], n1["accepted"]) == (MAX_ATTEMPTS, False)
    assert result.out.meta["nodes"][1]["outage"] is True
    assert "worker n2" in result.out.meta["llm_error"]
    assert result.out.file_quarantined
    assert not result.events("verified")
    (finished,) = result.events("run_finished")
    assert finished.status == "quarantined"  # type: ignore[attr-defined]
