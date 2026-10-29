"""Scripted fakes for the agent pipeline: a role-dispatching `FunctionModel` and a fake sandbox.

No network and no Docker: `Script` plays every role (dispatching on the role's system prompt) and
`FakeSandbox` maps known transform sources to canned Python callables. Generated code is never
executed (Global Constraints). Shared by `tests/agent/test_pipeline.py` and `tests/runs/`.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

import polars as pl
from model_fakes import output_part
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    SystemPromptPart,
    UserPromptPart,
)
from pydantic_ai.models.function import AgentInfo
from pydantic_ai.usage import RequestUsage

from sluice.agent.mapping import SYSTEM_PROMPT as MAPPER_PROMPT
from sluice.agent.plan import SYSTEM_PROMPT as PLANNER_PROMPT
from sluice.agent.verifier import VERIFIER_SYSTEM_PROMPT
from sluice.agent.worker import WORKER_SYSTEM_PROMPT
from sluice.sandbox.runner import SandboxResult

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
DIVIDE_100 = _module("    return values  # canned: paise -> rupees (see _paise_to_rupees)")
DIVIDE_100_LOSSY = _module("    return values  # canned: paise -> rupees, every 2nd value dropped")
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


def _paise_to_rupees(values: pl.Series, *, lossy: bool = False) -> pl.Series:
    out: list[str | None] = []
    for i, value in enumerate(values.to_list()):
        text = None if value is None else value.strip().replace(",", "")
        if text is None or text in {"N/A", "", "-1", "###"} or (lossy and i % 2):
            out.append(None)
            continue
        try:
            out.append(format((Decimal(text) / 100).quantize(Decimal("0.01")), "f"))
        except InvalidOperation:
            out.append(None)
    return pl.Series(values.name, out, dtype=pl.String)


CANNED: dict[str, Callable[[pl.Series], SandboxResult]] = {
    DIVIDE_100: lambda v: SandboxResult(True, _paise_to_rupees(v), None, 3),
    DIVIDE_100_LOSSY: lambda v: SandboxResult(True, _paise_to_rupees(v, lossy=True), None, 3),
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
            parts=[output_part(info, json.dumps(out))],
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
