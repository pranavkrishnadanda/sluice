"""The agent pipeline end to end with the real Docker sandbox and a scripted model (no network).

Needs Docker (Colima) and ``make sandbox-image``; run with ``make test-integration``. The model
is a Pydantic AI ``FunctionModel``; the worker's code really runs inside ``sluice-sandbox:dev``.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import polars as pl
import pytest
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    SystemPromptPart,
    TextPart,
    ToolCallPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.usage import RequestUsage

from sluice.agent.events import EventLog
from sluice.agent.pipeline import PipelineDeps, run_pipeline
from sluice.agent.plan import SYSTEM_PROMPT as PLANNER_PROMPT
from sluice.agent.verifier import VERIFIER_SYSTEM_PROMPT
from sluice.agent.worker import WORKER_SYSTEM_PROMPT
from sluice.contracts.model import load_contract
from sluice.drift.cases import Recipe, build_case
from sluice.evals.graders import grade
from sluice.evals.types import CaseInput
from sluice.llm.client import LLM
from sluice.sandbox.runner import run_transform
from sluice.systems.rules_only import RulesOnlySystem

pytestmark = pytest.mark.integration

CODE = (
    "import polars as pl\n"
    "\n"
    "\n"
    "def transform(values: pl.Series) -> pl.Series:\n"
    "    gone = values.str.strip_chars().is_in(['N/A', '', '-1', '###'])\n"
    "    fixed = values.str.replace_all(',', '')\n"
    "    return pl.select(pl.when(gone).then(None).otherwise(fixed)).to_series()\n"
)

ANSWERS: dict[str, dict[str, Any]] = {
    PLANNER_PROMPT: {
        "nodes": [
            {
                "id": "n1",
                "column": "o_totalprice",
                "issue": "Indian digit grouping",
                "difficulty": "easy",
                "depends_on": [],
            }
        ]
    },
    WORKER_SYSTEM_PROMPT: {"code": CODE, "explanation": "drop the grouping commas"},
    VERIFIER_SYSTEM_PROMPT: {"accept": True, "reason": "digits preserved"},
}


def _output_part(info: AgentInfo, payload: str) -> TextPart | ToolCallPart:
    """The model's structured answer: the output tool call in tool mode, else JSON text."""
    if info.output_tools:
        return ToolCallPart(info.output_tools[0].name, payload)
    return TextPart(payload)


def _respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    request = messages[0]
    assert isinstance(request, ModelRequest)
    system = next(p.content for p in request.parts if isinstance(p, SystemPromptPart))
    return ModelResponse(
        parts=[_output_part(info, json.dumps(ANSWERS[system]))],
        usage=RequestUsage(input_tokens=10, output_tokens=5),
    )


def test_pipeline_repairs_indian_numbers_in_the_real_sandbox(
    tmp_path: Path, orders_df: pl.DataFrame
) -> None:
    ops = [
        ("indian_number_string", {"column": "o_totalprice"}),
        ("destroy", {"column": "o_totalprice", "frac": 0.1, "token": "N/A"}),
    ]
    recipe = Recipe("orders", "medium", ops, "csv", "utf-8", 40)
    case = build_case("dev-0000", "dev", recipe, {"orders": orders_df}, 7, tmp_path)
    case_input = CaseInput(case.input_path, load_contract("orders"), {})

    async def go() -> Any:
        async with LLM("http://litellm.test", "k", model_override=FunctionModel(_respond)) as llm:
            deps = PipelineDeps(llm=llm, sandbox=run_transform, clock=time.monotonic)
            return await run_pipeline(case_input, deps, EventLog())

    out = asyncio.run(go())
    agent = grade(case, out, {})
    rules = grade(case, RulesOnlySystem().run(case_input), {})
    assert agent.cell_accuracy == 1.0
    assert rules.cell_accuracy is not None and rules.cell_accuracy < 1.0
    assert agent.fabrication_rate in (None, 0.0)
    assert [t["passed"] for t in out.meta["transforms"]] == [True]
