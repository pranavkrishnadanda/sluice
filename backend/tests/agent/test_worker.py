"""Worker: the transform-writing prompt, static code precheck and the fabrication guard.

No network: a Pydantic AI `FunctionModel` stands in for the LiteLLM proxy and captures the prompt.
Generated code is only ever parsed with `ast` here, never executed (Global Constraints).
"""

import asyncio
import json
from dataclasses import dataclass
from typing import Any

import polars as pl
import pytest
from pydantic import ValidationError
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.usage import RequestUsage

from sluice.agent import caps
from sluice.agent.worker import (
    DESTROYED_TOKENS,
    MAX_PROMPT_SAMPLES,
    WORKER_SYSTEM_PROMPT,
    TransformCode,
    guard_output,
    precheck_code,
    write_transform,
)
from sluice.contracts.model import ColumnSpec
from sluice.llm.client import LLM, CallRecord, LLMOutputError

BASE_URL = "http://litellm.test"
API_KEY = "sk-sluice-test-master-key-0123456789"

GOOD_CODE = (
    "import polars as pl\n"
    "\n"
    "def transform(values: pl.Series) -> pl.Series:\n"
    "    return values.str.replace_all(',', '')\n"
)


def _output_part(info: AgentInfo, payload: str) -> TextPart | ToolCallPart:
    """The model's structured answer: the output tool call in tool mode, else JSON text."""
    if info.output_tools:
        return ToolCallPart(info.output_tools[0].name, payload)
    return TextPart(payload)


@dataclass(frozen=True)
class Node:
    """Structural stand-in for `sluice.agent.plan.PlanNode` (Task 6)."""

    id: str
    column: str
    issue: str


NODE = Node(id="n1", column="o_totalprice", issue="Indian digit grouping like 1,24,500.50")
SPEC = ColumnSpec(name="o_totalprice", dtype="decimal", unit="INR", min=0)


class Capture:
    """A FunctionModel that records every request and answers with the next scripted output."""

    def __init__(self, *outputs: dict[str, Any]) -> None:
        self.queue = list(outputs)
        self.requests: list[list[ModelMessage]] = []
        self.model = FunctionModel(self._respond, model_name="capture")

    def _respond(self, messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        self.requests.append(list(messages))
        return ModelResponse(
            parts=[_output_part(info, json.dumps(self.queue.pop(0)))],
            usage=RequestUsage(input_tokens=40, output_tokens=20),
        )

    def text(self, request: int = 0) -> str:
        """All part contents (system + user + retry prompts) of one request, joined."""
        chunks: list[str] = []
        for message in self.requests[request]:
            for part in message.parts:
                content = getattr(part, "content", None)
                if isinstance(content, str):
                    chunks.append(content)
        return "\n".join(chunks)


def run(coro):  # noqa: ANN001, ANN201 - tiny sync shim, keeps tests free of an async plugin
    return asyncio.run(coro)


def _write(cap: Capture, **overrides: Any) -> tuple[TransformCode, CallRecord]:
    kwargs: dict[str, Any] = {
        "node": NODE,
        "spec": SPEC,
        "source_name": "amt_paise",
        "samples": ["1,24,500.50", "12,34,567.00"],
        "prior_error": None,
        "tier": "small",
    }
    kwargs.update(overrides)

    async def go() -> tuple[TransformCode, CallRecord]:
        async with LLM(BASE_URL, API_KEY, model_override=cap.model) as llm:
            return await write_transform(llm=llm, **kwargs)

    return run(go())


def ok(code: str = GOOD_CODE) -> dict[str, str]:
    return {"code": code, "explanation": "strip the digit-group commas"}


# ---------------------------------------------------------------- guard_output: fabrication


def s(*values: str | None) -> pl.Series:
    return pl.Series(list(values), dtype=pl.String)


def test_guard_output_destroyed_token_filled_is_rejected() -> None:
    err = guard_output(s("1,200.00", "N/A"), s("1200.00", "0.00"))
    assert err is not None
    assert "fabricat" in err
    assert "1" in err


def test_guard_output_null_filled_is_rejected() -> None:
    err = guard_output(s(None, "5"), s("1", "5"))
    assert err is not None
    assert "0" in err


@pytest.mark.parametrize("token", sorted(DESTROYED_TOKENS))
def test_guard_output_each_destroyed_token_filled_is_rejected(token: str) -> None:
    assert guard_output(s("7", token), s("7", "42")) is not None


@pytest.mark.parametrize("token", [" N/A ", "  ", "\t-1", "### "])
def test_guard_output_whitespace_padded_token_filled_is_rejected(token: str) -> None:
    assert guard_output(s(token), s("0")) is not None


def test_guard_output_tokens_and_nulls_mapped_to_null_are_accepted() -> None:
    raw = s("1,24,500.50", "N/A", "", "-1", "###", None, " N/A ")
    out = s("124500.50", None, None, None, None, None, None)
    assert guard_output(raw, out) is None


def test_guard_output_value_dropped_to_null_is_accepted() -> None:
    assert guard_output(s("garbage", "12"), s(None, "12")) is None


def test_guard_output_lookalike_values_are_not_tokens() -> None:
    # "-1.00", "N/A2" and "-10" are not destroyed tokens: rewriting them is allowed.
    assert guard_output(s("-1.00", "N/A2", "-10"), s("-1.00", "2", "-10")) is None


def test_guard_output_length_mismatch_is_rejected() -> None:
    err = guard_output(s("1", "2", "3"), s("1", "2"))
    assert err is not None
    assert "length" in err


def test_guard_output_non_string_output_is_rejected() -> None:
    err = guard_output(s("1", "2"), pl.Series([1, 2], dtype=pl.Int64))
    assert err is not None
    assert "String" in err


def test_guard_output_non_string_raw_is_rejected() -> None:
    assert guard_output(pl.Series([1, 2]), s("1", "2")) is not None


def test_guard_output_names_at_most_five_rows_and_the_total() -> None:
    raw = s(*(["N/A"] * 9))
    out = s(*(["0"] * 9))
    err = guard_output(raw, out)
    assert err is not None
    assert "9" in err
    assert "[0, 1, 2, 3, 4]" in err
    assert ", 5" not in err


def test_guard_output_empty_series_is_accepted() -> None:
    assert guard_output(s(), s()) is None


def test_guard_output_never_echoes_cell_values() -> None:
    err = guard_output(s("N/A"), s("SECRET-LOOKING-VALUE"))
    assert err is not None
    assert "SECRET-LOOKING-VALUE" not in err


# ---------------------------------------------------------------- precheck_code (ast only)


def test_precheck_code_accepts_minimal_transform() -> None:
    assert precheck_code(GOOD_CODE) is None


@pytest.mark.parametrize(
    "code",
    [
        "import re\nimport math\nfrom decimal import Decimal\nimport datetime\n" + GOOD_CODE,
        "from datetime import date, datetime\n" + GOOD_CODE,
        "import polars as pl\nimport re\n_X = re.compile(r'\\d')\n" + GOOD_CODE,
        "from __future__ import annotations\n" + GOOD_CODE,
    ],
)
def test_precheck_code_accepts_allowed_imports(code: str) -> None:
    assert precheck_code(code) is None


def test_precheck_code_without_transform_is_rejected() -> None:
    err = precheck_code("def convert(values):\n    return values\n")
    assert err is not None
    assert "transform" in err


def test_precheck_code_with_syntax_error_is_rejected() -> None:
    err = precheck_code("def transform(values:\n")
    assert err is not None
    assert "syntax" in err.lower()


def test_precheck_code_nested_transform_does_not_count() -> None:
    code = "def outer():\n    def transform(values):\n        return values\n"
    assert precheck_code(code) is not None


def test_precheck_code_async_transform_is_rejected() -> None:
    assert precheck_code("async def transform(values):\n    return values\n") is not None


def test_precheck_code_transform_with_wrong_arity_is_rejected() -> None:
    assert precheck_code("def transform(a, b):\n    return a\n") is not None


@pytest.mark.parametrize(
    "imp",
    [
        "import os",
        "import subprocess",
        "from os import path",
        "import polars.io as pio",
        "import socket, re",
        "from . import x",
        "import importlib",
        "from __future__ import generator_stop",
        "import __future__",
    ],
)
def test_precheck_code_disallowed_import_is_rejected(imp: str) -> None:
    err = precheck_code(imp + "\n" + GOOD_CODE)
    assert err is not None
    assert "import" in err


def test_precheck_code_import_inside_function_is_rejected() -> None:
    code = "def transform(values):\n    import os\n    return values\n"
    assert precheck_code(code) is not None


@pytest.mark.parametrize(
    "body",
    [
        "__import__('os')",
        "eval('1')",
        "exec('x = 1')",
        "compile('1', 'f', 'eval')",
        "open('/etc/passwd')",
        "getattr(values, 'x')",
        "globals()",
        "values.__class__.__subclasses__()",
        "breakpoint()",
    ],
)
def test_precheck_code_dangerous_constructs_are_rejected(body: str) -> None:
    code = f"def transform(values):\n    {body}\n    return values\n"
    assert precheck_code(code) is not None


def test_precheck_code_oversized_code_is_rejected() -> None:
    code = GOOD_CODE + "#" * 50_000
    err = precheck_code(code)
    assert err is not None
    assert "long" in err


def test_precheck_code_never_executes_code(tmp_path) -> None:  # noqa: ANN001
    marker = tmp_path / "ran"
    code = f"open({str(marker)!r}, 'w').write('x')\n" + GOOD_CODE
    precheck_code(code)
    assert not marker.exists()


# ---------------------------------------------------------------- TransformCode


def test_transform_code_rejects_code_failing_precheck() -> None:
    with pytest.raises(ValidationError):
        TransformCode(code="def convert(v):\n    return v\n", explanation="x")


def test_transform_code_is_frozen_and_forbids_extra() -> None:
    with pytest.raises(ValidationError):
        TransformCode(code=GOOD_CODE, explanation="x", extra="y")  # type: ignore[call-arg]


# ---------------------------------------------------------------- write_transform: prompt


def test_write_transform_returns_code_and_record() -> None:
    cap = Capture(ok())
    code, record = _write(cap)
    assert code.code == GOOD_CODE
    assert record.role == "worker"
    assert record.tier == "small"


def test_write_transform_uses_requested_tier() -> None:
    cap = Capture(ok())
    _, record = _write(cap, tier="strong")
    assert record.tier == "strong"


def test_write_transform_prompt_states_contract_dtype_source_and_samples() -> None:
    cap = Capture(ok())
    _write(cap)
    text = cap.text()
    assert "decimal" in text
    assert "amt_paise" in text
    assert "o_totalprice" in text
    assert "1,24,500.50" in text
    assert "INR" in text
    assert "Indian digit grouping" in text
    assert "transform(values: pl.Series) -> pl.Series" in text
    assert "124500.50" in text


def test_write_transform_prompt_contains_prior_error() -> None:
    cap = Capture(ok())
    _write(cap, prior_error="verifier: values look 100x too large (paise)")
    assert "values look 100x too large (paise)" in cap.text()


def test_write_transform_prompt_without_prior_error_says_first_attempt() -> None:
    cap = Capture(ok())
    _write(cap)
    assert "first attempt" in cap.text().lower()


def test_write_transform_prior_error_is_truncated() -> None:
    cap = Capture(ok())
    _write(cap, prior_error="E" * (caps.MAX_ERROR_CHARS * 3))
    assert "E" * (caps.MAX_ERROR_CHARS + 1) not in cap.text()


def test_write_transform_samples_are_capped() -> None:
    cap = Capture(ok())
    samples = [f"sample-{i:03d}" for i in range(MAX_PROMPT_SAMPLES + 10)]
    _write(cap, samples=samples)
    text = cap.text()
    assert f"sample-{MAX_PROMPT_SAMPLES - 1:03d}" in text
    assert f"sample-{MAX_PROMPT_SAMPLES:03d}" not in text


@pytest.mark.parametrize(
    ("dtype", "expected"),
    [
        ("date", "YYYY-MM-DD"),
        ("timestamp_utc", "+05:30"),
        ("int64", "-?digits"),
    ],
)
def test_write_transform_prompt_names_target_format_per_dtype(dtype: str, expected: str) -> None:
    cap = Capture(ok())
    _write(cap, spec=ColumnSpec(name="c", dtype=dtype))  # type: ignore[arg-type]
    assert expected in cap.text()


def test_write_transform_system_prompt_rules() -> None:
    for phrase in ("String", "same length", "null", "Never invent", "N/A"):
        assert phrase in WORKER_SYSTEM_PROMPT


def test_write_transform_code_without_transform_is_retried_with_error() -> None:
    cap = Capture(ok("def convert(v):\n    return v\n"), ok())
    code, _ = _write(cap)
    assert code.code == GOOD_CODE
    assert len(cap.requests) == 2
    assert "transform" in cap.text(1)


def test_write_transform_code_invalid_twice_raises_output_error() -> None:
    bad = ok("import os\n" + GOOD_CODE)
    cap = Capture(bad, bad)
    with pytest.raises(LLMOutputError):
        _write(cap)
