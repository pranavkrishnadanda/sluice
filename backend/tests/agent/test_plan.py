"""Planner and router: candidate selection (pure code), plan validation, static tier cascade.

No network: Pydantic AI `FunctionModel` stands in for the orchestrator model.
"""

import asyncio
import json
from typing import Any

import polars as pl
import pytest
from model_fakes import output_part
from pydantic import ValidationError
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    UserPromptPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.usage import RequestUsage

from sluice.agent.plan import (
    MAX_SAMPLES,
    PLAN_FEEDBACK_CHARS,
    Plan,
    PlanError,
    PlanNode,
    candidate_columns,
    difficulty_floor,
    propose_plan,
    route_tier,
    validate_plan,
)
from sluice.contracts.model import Contract, load_contract
from sluice.llm.client import LLM, LLMOutputError, LLMUnavailable
from sluice.rules.fixers import RuleResult, apply_rules
from sluice.rules.profile import profile


@pytest.fixture(scope="module")
def orders() -> Contract:
    return load_contract("orders")


def rules(raw: pl.DataFrame, contract: Contract) -> RuleResult:
    return apply_rules(raw, contract, profile(raw, contract), {})


def frame(n: int, **cols: list[str | None]) -> pl.DataFrame:
    """All-String orders-like frame with keys 1..n plus the given columns."""
    data: dict[str, list[str | None]] = {"o_orderkey": [str(i) for i in range(1, n + 1)]}
    return pl.DataFrame(data | cols, schema=dict.fromkeys([*data, *cols], pl.String))


def run(coro):  # noqa: ANN001, ANN201 - tiny sync shim, keeps tests free of an async plugin
    return asyncio.run(coro)


# ---------------------------------------------------------------- candidate_columns


def test_candidates_include_indian_comma_totalprice(orders: Contract) -> None:
    raw = frame(3, o_totalprice=["1,24,500.50", "100.00", "N/A"])

    got = candidate_columns(raw, rules(raw, orders), orders)

    assert got["o_totalprice"] == ["1,24,500.50"]


def test_candidates_exclude_column_whose_flags_are_all_destroyed_tokens(orders: Contract) -> None:
    raw = frame(6, o_custkey=["N/A", "", "-1", "###", None, " N/A "])

    got = candidate_columns(raw, rules(raw, orders), orders)

    assert "o_custkey" not in got


def test_candidates_exclude_missing_and_unflagged_columns(orders: Contract) -> None:
    raw = frame(2, o_custkey=["1", "2"])  # o_orderdate etc. absent -> flagged on every row

    got = candidate_columns(raw, rules(raw, orders), orders)

    assert got == {}


def test_candidates_samples_are_sorted_distinct_and_capped(orders: Contract) -> None:
    bad = [f"{i:02d}/13/1996" for i in range(20)]  # month 13: never parses
    raw = frame(22, o_orderdate=[*bad, bad[0], "1996-01-02"])

    got = candidate_columns(raw, rules(raw, orders), orders)

    assert got["o_orderdate"] == sorted(set(bad))[:MAX_SAMPLES]
    assert MAX_SAMPLES == 12


def test_candidates_keep_raw_text_verbatim_and_follow_contract_order(orders: Contract) -> None:
    raw = frame(2, o_shippriority=[" zero ", "0"], o_totalprice=["1,000.00", "1.00"])

    got = candidate_columns(raw, rules(raw, orders), orders)

    assert list(got) == ["o_totalprice", "o_shippriority"]
    assert got["o_shippriority"] == [" zero "]


def test_candidates_find_columns_matched_by_normalised_name(orders: Contract) -> None:
    raw = frame(1, **{"O-TotalPrice": ["12,34,567.00"]})

    got = candidate_columns(raw, rules(raw, orders), orders)

    assert got == {"o_totalprice": ["12,34,567.00"]}


def test_candidates_ignore_rows_whose_key_cannot_be_parsed(orders: Contract) -> None:
    raw = pl.DataFrame({"o_orderkey": ["1", "bad"], "o_totalprice": ["1.00", "9,99.00"]})

    got = candidate_columns(raw, rules(raw, orders), orders)

    assert got == {}


def test_candidates_sample_only_the_duplicate_row_the_rules_kept(orders: Contract) -> None:
    # Same PK twice, one flag each: the rules keep the earliest row, whose price is destroyed.
    raw = pl.DataFrame({"o_orderkey": ["1", "1"], "o_totalprice": ["N/A", "1,00.00"]})

    got = candidate_columns(raw, rules(raw, orders), orders)

    assert "o_totalprice" not in got


def test_candidates_ignore_garbage_in_a_dropped_duplicate(orders: Contract) -> None:
    # Both rows' prices are flagged (an exact duplicate after repair): only row 0 survives.
    raw = pl.DataFrame({"o_orderkey": ["1", "1"], "o_totalprice": ["1,00.00", "zzz"]})

    got = candidate_columns(raw, rules(raw, orders), orders)

    assert got == {"o_totalprice": ["1,00.00"]}


def test_candidates_keep_the_more_flagged_duplicate(orders: Contract) -> None:
    raw = pl.DataFrame(
        {"o_orderkey": ["1", "1"], "o_custkey": ["7", "N/A"], "o_totalprice": ["1.00", "9,9.00"]}
    )

    got = candidate_columns(raw, rules(raw, orders), orders)

    assert got == {"o_totalprice": ["9,9.00"]}


# ---------------------------------------------------------------- plan models / validation


def node(nid: str, column: str, *deps: str, difficulty: str = "easy") -> dict[str, Any]:
    return {
        "id": nid,
        "column": column,
        "issue": f"{column} needs repair",
        "difficulty": difficulty,
        "depends_on": list(deps),
    }


CANDS = {"o_totalprice": ["1,24,500.50"], "o_orderdate": ["1996/01/02"]}


@pytest.mark.parametrize("bad_id", ["", "has space", "a" * 65, "semi;colon", "dot.id"])
def test_plan_node_rejects_bad_ids(bad_id: str) -> None:
    with pytest.raises(ValidationError):
        PlanNode.model_validate(node(bad_id, "o_totalprice"))


def test_plan_node_rejects_bad_dependency_id() -> None:
    with pytest.raises(ValidationError):
        PlanNode.model_validate(node("n1", "o_totalprice", "bad id"))


def test_validate_plan_accepts_dag() -> None:
    plan = Plan.model_validate(
        {"nodes": [node("a", "o_totalprice"), node("b", "o_orderdate", "a")]}
    )

    assert validate_plan(plan, CANDS) is plan


@pytest.mark.parametrize(
    ("nodes", "message"),
    [
        ([node("a", "o_custkey")], "not a candidate"),
        ([node("a", "o_totalprice"), node("b", "o_totalprice")], "more than one node"),
        ([node("a", "o_totalprice"), node("a", "o_orderdate")], "duplicate node id"),
        ([node("a", "o_totalprice", "zz")], "unknown node"),
        ([node("a", "o_totalprice", "a")], "cycle"),
        ([node("a", "o_totalprice", "b"), node("b", "o_orderdate", "a")], "cycle"),
    ],
)
def test_validate_plan_rejects(nodes: list[dict[str, Any]], message: str) -> None:
    plan = Plan.model_validate({"nodes": nodes})

    with pytest.raises(PlanError, match=message):
        validate_plan(plan, CANDS)


def test_validate_plan_handles_long_chains_without_recursion() -> None:
    n = 5000
    cands = {f"c{i}": ["x"] for i in range(n)}
    nodes = [node(f"n{i}", f"c{i}", *([f"n{i - 1}"] if i else [])) for i in range(n)]
    plan = Plan.model_validate({"nodes": nodes})

    assert validate_plan(plan, cands) is plan


# ---------------------------------------------------------------- propose_plan


class Scripted:
    """A FunctionModel answering each run with the next plan; records every user prompt."""

    def __init__(self, *plans: dict[str, Any]) -> None:
        self.queue = list(plans)
        self.prompts: list[str] = []

    def respond(self, messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        for msg in messages:
            if isinstance(msg, ModelRequest):
                self.prompts += [str(p.content) for p in msg.parts if isinstance(p, UserPromptPart)]
        return ModelResponse(
            parts=[output_part(info, json.dumps(self.queue.pop(0)))],
            usage=RequestUsage(input_tokens=40, output_tokens=8),
        )

    def llm(self) -> LLM:
        return LLM("http://litellm.test", "k", model_override=FunctionModel(self.respond))


def test_propose_plan_no_candidates_makes_no_call(orders: Contract) -> None:
    script = Scripted()  # an empty queue: any call would raise

    plan, records = run(propose_plan({}, orders, script.llm()))

    assert plan == Plan(nodes=[])
    assert records == []
    assert script.prompts == []


def test_propose_plan_returns_valid_plan_on_strong_tier(orders: Contract) -> None:
    script = Scripted({"nodes": [node("price", "o_totalprice", difficulty="hard")]})

    plan, records = run(propose_plan(CANDS, orders, script.llm()))

    assert [n.column for n in plan.nodes] == ["o_totalprice"]
    assert [(r.role, r.tier) for r in records] == [("orchestrator", "strong")]
    assert "1,24,500.50" in script.prompts[0]
    assert "o_orderdate" in script.prompts[0]


def test_propose_plan_rejects_non_candidate_then_retries_with_error(orders: Contract) -> None:
    script = Scripted(
        {"nodes": [node("a", "o_custkey")]},
        {"nodes": [node("a", "o_totalprice")]},
    )

    plan, records = run(propose_plan(CANDS, orders, script.llm()))

    assert [n.column for n in plan.nodes] == ["o_totalprice"]
    assert len(records) == 2
    assert "not a candidate" in script.prompts[-1]


def test_propose_plan_cycle_twice_raises_output_error(orders: Contract) -> None:
    cyclic = {"nodes": [node("a", "o_totalprice", "b"), node("b", "o_orderdate", "a")]}
    script = Scripted(cyclic, cyclic)

    with pytest.raises(LLMOutputError, match="cycle") as info:
        run(propose_plan(CANDS, orders, script.llm()))

    assert info.value.record.tier == "strong"
    assert len(script.prompts) == 2


def test_propose_plan_may_return_empty_plan(orders: Contract) -> None:
    script = Scripted({"nodes": []})

    plan, records = run(propose_plan(CANDS, orders, script.llm()))

    assert plan.nodes == []
    assert len(records) == 1


def test_propose_plan_retries_after_client_output_error(orders: Contract) -> None:
    bad = {"nodes": [node("bad id", "o_totalprice")]}  # fails the client's schema check twice
    script = Scripted(bad, bad, {"nodes": [node("a", "o_totalprice")]})

    plan, records = run(propose_plan(CANDS, orders, script.llm()))

    assert [n.column for n in plan.nodes] == ["o_totalprice"]
    assert len(records) == 2
    assert "rejected" in script.prompts[-1]
    assert "bad id" not in script.prompts[-1]


def test_propose_plan_client_output_error_twice_raises(orders: Contract) -> None:
    bad = {"nodes": [node("bad id", "o_totalprice")]}
    script = Scripted(bad, bad, bad, bad)

    with pytest.raises(LLMOutputError) as info:
        run(propose_plan(CANDS, orders, script.llm()))

    assert info.value.record.tier == "strong"
    assert info.value.__context__ is None
    assert script.queue == []


def test_propose_plan_unavailable_propagates_without_retry(orders: Contract) -> None:
    calls = 0

    def down(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        nonlocal calls
        calls += 1
        raise ModelHTTPError(503, "strong")

    llm = LLM("http://litellm.test", "k", model_override=FunctionModel(down))

    with pytest.raises(LLMUnavailable):
        run(propose_plan(CANDS, orders, llm))

    assert calls == 1


def test_propose_plan_feedback_is_bounded(orders: Contract) -> None:
    long = "z" * 128
    script = Scripted({"nodes": [node("a", long)]}, {"nodes": []})

    run(propose_plan(CANDS, orders, script.llm()))

    feedback = script.prompts[-1].split("rejected:", 1)[1]
    assert len(feedback) <= PLAN_FEEDBACK_CHARS + len("\nReturn a corrected plan.")
    assert long not in feedback


# ---------------------------------------------------------------- route_tier


@pytest.mark.parametrize(
    ("difficulty", "attempt", "tier"),
    [
        ("easy", 1, "small"),
        ("easy", 2, "strong"),
        ("easy", 3, "strong"),
        ("hard", 1, "strong"),
        ("hard", 2, "strong"),
        ("hard", 3, "strong"),
    ],
)
def test_route_tier_static_cascade(difficulty: str, attempt: int, tier: str) -> None:
    n = PlanNode.model_validate(node("n", "o_totalprice", difficulty=difficulty))

    assert route_tier(n, attempt) == tier


@pytest.mark.parametrize("attempt", [0, -1])
def test_route_tier_rejects_non_positive_attempt(attempt: int) -> None:
    n = PlanNode.model_validate(node("n", "o_totalprice"))

    with pytest.raises(ValueError, match="attempt"):
        route_tier(n, attempt)


# ---------------------------------------------------------------- H2b: unit issues


def _paise_texts(n: int) -> list[str | None]:
    return [f"{100_000 + 1_237 * i}.00" for i in range(n)]  # parsed, in range


def test_candidates_include_a_unit_issue_column_without_flags(orders: Contract) -> None:
    raw = frame(60, o_totalprice=_paise_texts(60))
    result = rules(raw, orders)
    assert not any(col == "o_totalprice" for _, col in result.flags)

    got = candidate_columns(raw, result, orders)

    assert list(got) == ["o_totalprice"]
    assert got["o_totalprice"] == sorted(set(_paise_texts(60)))[:MAX_SAMPLES]  # all rows


def test_candidates_unit_issue_samples_skip_destroyed_tokens(orders: Contract) -> None:
    raw = frame(64, o_totalprice=["N/A", "", None, "-1", *_paise_texts(60)])
    got = candidate_columns(raw, rules(raw, orders), orders)
    assert not {"N/A", "", "-1"} & set(got["o_totalprice"])


def test_propose_plan_prompt_carries_the_profiler_issue(orders: Contract) -> None:
    script = Scripted({"nodes": [node("n1", "o_totalprice")]})
    issues = {"o_totalprice": "values look like paise: 100% whole numbers, median 1036492"}

    run(propose_plan(CANDS, orders, script.llm(), issues=issues))

    (prompt,) = script.prompts
    assert json.dumps(issues["o_totalprice"]) in prompt
    data = json.loads(prompt.split("\n", 1)[1].split("\n\n")[0])
    by_column = {c["column"]: c for c in data["candidates"]}
    assert by_column["o_totalprice"]["profiler_issue"] == issues["o_totalprice"]
    assert by_column["o_orderdate"]["profiler_issue"] is None


# ---------------------------------------------------------------- H4: difficulty floor


def test_difficulty_floor_unit_issue_is_hard(orders: Contract) -> None:
    spec = orders.column("o_totalprice")
    assert difficulty_floor(spec, ["12450050"], unit_issue=True) == "hard"


def test_difficulty_floor_two_date_formats_is_hard(orders: Contract) -> None:
    spec = orders.column("o_orderdate")
    assert difficulty_floor(spec, ["02 Jan 1996", "1996/01/02"], unit_issue=False) == "hard"
    assert difficulty_floor(spec, ["02 Jan 1996", "Jan 5, 1996"], unit_issue=False) == "hard"


def test_difficulty_floor_one_date_format_is_easy(orders: Contract) -> None:
    spec = orders.column("o_orderdate")
    samples = ["02 Jan 1996", "17 Mar 1994", "5 Dec 1998"]
    assert difficulty_floor(spec, samples, unit_issue=False) == "easy"


def test_difficulty_floor_separators_and_rescaling_is_hard(orders: Contract) -> None:
    spec = orders.column("o_totalprice")
    grouped = ["1,24,50,050", "3,10,000"]
    assert difficulty_floor(spec, grouped, unit_issue=False, source_name="amt_paise") == "hard"
    assert difficulty_floor(spec, grouped, unit_issue=False, source_name="amt") == "easy"
    assert difficulty_floor(spec, ["12450050"], unit_issue=False, source_name="amt_paise") == (
        "easy"
    )


def test_propose_plan_raises_an_easy_node_to_the_floor(orders: Contract) -> None:
    script = Scripted({"nodes": [node("n1", "o_totalprice"), node("n2", "o_orderdate")]})
    cands = {"o_totalprice": ["1,24,50,050"], "o_orderdate": ["02 Jan 1996"]}
    issues = {"o_totalprice": "values look like paise: 100% whole numbers, median 1036492"}

    plan, _ = run(propose_plan(cands, orders, script.llm(), issues=issues))

    assert [n.difficulty for n in plan.nodes] == ["hard", "easy"]
    assert route_tier(plan.nodes[0], 1) == "strong"


def test_propose_plan_floor_never_lowers_the_orchestrators_difficulty(orders: Contract) -> None:
    script = Scripted({"nodes": [node("n1", "o_orderdate", difficulty="hard")]})
    plan, _ = run(propose_plan({"o_orderdate": ["02 Jan 1996"]}, orders, script.llm()))
    assert plan.nodes[0].difficulty == "hard"


def test_propose_plan_floor_uses_the_source_column_name(orders: Contract) -> None:
    script = Scripted({"nodes": [node("n1", "o_totalprice")]})
    cands = {"o_totalprice": ["1,24,50,050"]}
    plan, _ = run(
        propose_plan(cands, orders, script.llm(), source_names={"o_totalprice": "price_paise"})
    )
    assert plan.nodes[0].difficulty == "hard"


# ---------------------------------------------------------------- U1: unit columns need a node

PAISE_ISSUE = {"o_totalprice": "values look like paise: 100% whole numbers, median ≈ 1 million"}


def test_validate_plan_rejects_a_plan_without_a_node_for_a_unit_column() -> None:
    plan = Plan.model_validate({"nodes": [node("a", "o_orderdate")]})

    with pytest.raises(PlanError, match="o_totalprice.*unit issue"):
        validate_plan(plan, CANDS, required={"o_totalprice"})


def test_validate_plan_accepts_a_plan_covering_every_unit_column() -> None:
    plan = Plan.model_validate({"nodes": [node("a", "o_totalprice")]})

    assert validate_plan(plan, CANDS, required={"o_totalprice"}) is plan


def test_propose_plan_re_asks_once_when_a_unit_column_has_no_node(orders: Contract) -> None:
    script = Scripted({"nodes": []}, {"nodes": [node("a", "o_totalprice")]})

    plan, records = run(propose_plan(CANDS, orders, script.llm(), issues=PAISE_ISSUE))

    assert [n.column for n in plan.nodes] == ["o_totalprice"]
    assert len(records) == 2
    assert "unit issue" in script.prompts[-1]


def test_propose_plan_raises_when_a_unit_column_is_omitted_twice(orders: Contract) -> None:
    script = Scripted({"nodes": []}, {"nodes": []})

    with pytest.raises(LLMOutputError, match="unit issue"):
        run(propose_plan(CANDS, orders, script.llm(), issues=PAISE_ISSUE))
