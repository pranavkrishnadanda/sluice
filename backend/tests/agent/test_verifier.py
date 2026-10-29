"""Verifier: an independent `strong` judge of a node's raw -> rewritten pairs and stats."""

import asyncio
import json
from dataclasses import dataclass
from typing import Any

import pytest
from model_fakes import output_part
from pydantic import ValidationError
from pydantic_ai.messages import ModelMessage, ModelResponse
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.usage import RequestUsage

from sluice.agent import caps
from sluice.agent.verifier import (
    MAX_VERIFY_PAIRS,
    VERIFIER_SYSTEM_PROMPT,
    Verdict,
    verify_node,
)
from sluice.contracts.model import ColumnSpec
from sluice.llm.client import LLM, CallRecord

BASE_URL = "http://litellm.test"
API_KEY = "sk-sluice-test-master-key-0123456789"


@dataclass(frozen=True)
class Node:
    id: str
    column: str
    issue: str


NODE = Node(id="n1", column="o_totalprice", issue="amounts in paise")
SPEC = ColumnSpec(name="o_totalprice", dtype="decimal", unit="INR", min=0, max=600000)
PAIRS: list[tuple[str, str | None]] = [("12450050", "124500.50"), ("N/A", None)]
STATS: dict[str, float | int | None] = {
    "before_parsed_max": None,
    "after_parsed_max": 124500.5,
    "after_null_count": 1,
}


class Capture:
    def __init__(self, *outputs: dict[str, Any]) -> None:
        self.queue = list(outputs)
        self.requests: list[list[ModelMessage]] = []
        self.model = FunctionModel(self._respond, model_name="capture")

    def _respond(self, messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        self.requests.append(list(messages))
        return ModelResponse(
            parts=[output_part(info, json.dumps(self.queue.pop(0)))],
            usage=RequestUsage(input_tokens=30, output_tokens=5),
        )

    def text(self, request: int = 0) -> str:
        chunks: list[str] = []
        for message in self.requests[request]:
            for part in message.parts:
                content = getattr(part, "content", None)
                if isinstance(content, str):
                    chunks.append(content)
        return "\n".join(chunks)


def _verify(cap: Capture, **overrides: Any) -> tuple[Verdict, CallRecord]:
    kwargs: dict[str, Any] = {"node": NODE, "spec": SPEC, "pairs": PAIRS, "stats": STATS}
    kwargs.update(overrides)

    async def go() -> tuple[Verdict, CallRecord]:
        async with LLM(BASE_URL, API_KEY, model_override=cap.model) as llm:
            return await verify_node(llm=llm, **kwargs)

    return asyncio.run(go())


@pytest.mark.parametrize("accept", [True, False])
def test_verify_node_returns_model_verdict(accept: bool) -> None:
    cap = Capture({"accept": accept, "reason": "values plausible" if accept else "100x too large"})
    verdict, _ = _verify(cap)
    assert verdict.accept is accept


def test_verify_node_runs_on_strong_tier_as_verifier() -> None:
    cap = Capture({"accept": True, "reason": "ok"})
    _, record = _verify(cap)
    assert record.tier == "strong"
    assert record.role == "verifier"


def test_verify_node_prompt_contains_pairs_stats_and_node() -> None:
    cap = Capture({"accept": True, "reason": "ok"})
    _verify(cap)
    text = cap.text()
    assert "12450050" in text
    assert "124500.50" in text
    assert "after_parsed_max" in text
    assert "o_totalprice" in text
    assert "amounts in paise" in text


def test_verify_node_sees_at_most_twenty_pairs() -> None:
    cap = Capture({"accept": True, "reason": "ok"})
    pairs = [(f"raw-{i:03d}", f"out-{i:03d}") for i in range(MAX_VERIFY_PAIRS + 15)]
    _verify(cap, pairs=pairs)
    text = cap.text()
    assert MAX_VERIFY_PAIRS == 20
    assert f"raw-{MAX_VERIFY_PAIRS - 1:03d}" in text
    assert f"raw-{MAX_VERIFY_PAIRS:03d}" not in text


def test_verify_node_reason_is_truncated() -> None:
    cap = Capture({"accept": False, "reason": "R" * (caps.MAX_ERROR_CHARS * 2)})
    verdict, _ = _verify(cap)
    assert len(verdict.reason) <= caps.MAX_ERROR_CHARS


def test_verdict_rejects_empty_reason() -> None:
    with pytest.raises(ValidationError):
        Verdict(accept=True, reason="")


def test_verifier_system_prompt_mentions_unit_scale_and_fabrication() -> None:
    assert "100" in VERIFIER_SYSTEM_PROMPT
    assert "fabricat" in VERIFIER_SYSTEM_PROMPT.lower()


def test_verify_node_prompt_contains_spec_dtype_unit_and_range() -> None:
    cap = Capture({"accept": True, "reason": "ok"})
    _verify(cap)
    text = cap.text()
    assert '"dtype": "decimal"' in text
    assert '"unit": "INR"' in text
    assert '"min": 0' in text
    assert '"max": 600000' in text


def test_verify_node_prompt_contains_enum_and_omits_unset_fields() -> None:
    cap = Capture({"accept": True, "reason": "ok"})
    spec = ColumnSpec(name="o_orderstatus", dtype="string", enum=["F", "O", "P"])
    _verify(cap, spec=spec)
    text = cap.text()
    assert '"enum": ["F", "O", "P"]' in text
    assert '"unit"' not in text
    assert '"min"' not in text
    assert '"max"' not in text


def test_verifier_prompt_explains_each_row_count_base() -> None:
    for key in ("rows_raw", "rows_after_dedupe", "non_null_after", "null_after"):
        assert key in VERIFIER_SYSTEM_PROMPT
    assert "duplicate" in VERIFIER_SYSTEM_PROMPT


def test_verifier_prompt_rejects_money_still_in_whole_paise() -> None:
    for key in ("median_before", "median_after", "whole_share_after", "p95_after"):
        assert key in VERIFIER_SYSTEM_PROMPT
    assert "whole-number paise" in VERIFIER_SYSTEM_PROMPT
