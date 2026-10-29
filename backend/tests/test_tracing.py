"""OpenTelemetry spans: setup, the run/step/node-attempt tree, LLM calls and sandbox runs.

Spans go to an in-memory exporter on a process-wide SDK provider (installed once for the
session; the OpenTelemetry API allows one global provider). No network, no Docker: the pipeline
runs on a scripted `FunctionModel`, and the real sandbox runner on a fake Docker client that
plays the container from the test process (generated code never runs).
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import docker.errors
import polars as pl
import pytest
import requests
from model_fakes import output_part
from opentelemetry import trace
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode
from pydantic import BaseModel
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    SystemPromptPart,
    UserPromptPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.usage import RequestUsage

from sluice import tracing
from sluice.agent.events import EventModel
from sluice.agent.mapping import SYSTEM_PROMPT as MAPPER_PROMPT
from sluice.agent.plan import SYSTEM_PROMPT as PLANNER_PROMPT
from sluice.agent.verifier import VERIFIER_SYSTEM_PROMPT
from sluice.agent.worker import WORKER_SYSTEM_PROMPT
from sluice.drift.cases import Case, Recipe, build_case
from sluice.evals.types import CaseInput, SystemOutput
from sluice.llm.client import LLM, LLMOutputError, LLMUnavailable
from sluice.load.iceberg import LoadResult
from sluice.runs.execute import RunOutcome, execute_run
from sluice.sandbox import runner
from sluice.systems.agent import AgentSystem

API_KEY = "sk-sluice-trace-test-key-9f8e7d6c5b4a"


# --------------------------------------------------------------------------- exporter


@pytest.fixture(scope="session")
def _session_exporter() -> InMemorySpanExporter:
    exporter = InMemorySpanExporter()
    current = trace.get_tracer_provider()
    if isinstance(current, TracerProvider):
        current.add_span_processor(SimpleSpanProcessor(exporter))
    else:
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        trace.set_tracer_provider(provider)
    return exporter


@pytest.fixture
def spans(_session_exporter: InMemorySpanExporter) -> Iterator[InMemorySpanExporter]:
    _session_exporter.clear()
    yield _session_exporter
    _session_exporter.clear()


def _named(exporter: InMemorySpanExporter, name: str) -> list[ReadableSpan]:
    return [s for s in exporter.get_finished_spans() if s.name == name]


def _all_values(exporter: InMemorySpanExporter) -> list[str]:
    """Every attribute, event and status text of every finished span, as strings."""
    out: list[str] = []
    for s in exporter.get_finished_spans():
        out.extend(str(v) for v in (s.attributes or {}).values())
        out.extend(str(v) for e in s.events for v in (e.attributes or {}).values())
        out.append(s.status.description or "")
        out.append(s.name)
    return out


# --------------------------------------------------------------------------- setup_tracing


@pytest.fixture
def fresh_setup(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[object]:
    """Record instead of installing a global provider; reset the idempotency latch."""
    installed: list[object] = []
    monkeypatch.setattr(tracing, "_installed", False)
    monkeypatch.setattr(tracing.trace, "set_tracer_provider", installed.append)
    monkeypatch.delenv("OTEL_SERVICE_NAME", raising=False)
    # Never the developer's real backend/.env: a path that does not exist unless a test writes it.
    monkeypatch.setattr(tracing, "ENV_FILE", tmp_path / "absent.env")
    return installed


def _write_env(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def _endpoint_of(provider: object) -> str:
    (processor,) = provider._active_span_processor._span_processors  # type: ignore[attr-defined]
    exporter = getattr(processor, "span_exporter", None) or processor._batch_processor._exporter
    return exporter._endpoint  # type: ignore[no-any-return]


def test_setup_tracing_reads_the_endpoint_from_the_env_file(
    fresh_setup: list[object], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    env = _write_env(
        tmp_path / "file.env",
        "POSTGRES_PASSWORD=x\nOTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:9/\n"
        "OTEL_SERVICE_NAME=from-file\n",
    )
    monkeypatch.setattr(tracing, "ENV_FILE", env)
    tracing.setup_tracing()
    try:
        (provider,) = fresh_setup
        assert isinstance(provider, TracerProvider)
        assert provider.resource.attributes["service.name"] == "from-file"
        assert _endpoint_of(provider) == "http://127.0.0.1:9/v1/traces"
    finally:
        for p in fresh_setup:
            p.shutdown()  # type: ignore[attr-defined]


def test_setup_tracing_process_env_overrides_the_env_file(
    fresh_setup: list[object], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    env = _write_env(tmp_path / "file.env", "OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:9\n")
    monkeypatch.setattr(tracing, "ENV_FILE", env)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:10")
    tracing.setup_tracing()
    try:
        (provider,) = fresh_setup
        assert _endpoint_of(provider) == "http://127.0.0.1:10/v1/traces"
    finally:
        for p in fresh_setup:
            p.shutdown()  # type: ignore[attr-defined]


def test_setup_tracing_env_file_without_endpoint_installs_nothing(
    fresh_setup: list[object], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    env = _write_env(tmp_path / "file.env", "# OTEL_EXPORTER_OTLP_ENDPOINT=http://x\nA=1\n")
    monkeypatch.setattr(tracing, "ENV_FILE", env)
    tracing.setup_tracing()
    assert fresh_setup == []


def test_setup_tracing_without_endpoint_installs_nothing(
    fresh_setup: list[object], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    tracing.setup_tracing()
    assert fresh_setup == []


def test_setup_tracing_with_blank_endpoint_installs_nothing(
    fresh_setup: list[object], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "  ")
    tracing.setup_tracing()
    assert fresh_setup == []


def test_setup_tracing_with_endpoint_installs_one_provider_once(
    fresh_setup: list[object], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:9")
    tracing.setup_tracing()
    tracing.setup_tracing()
    try:
        (provider,) = fresh_setup
        assert isinstance(provider, TracerProvider)
        assert provider.resource.attributes["service.name"] == "sluice"
    finally:
        for p in fresh_setup:
            p.shutdown()  # type: ignore[attr-defined]  # stops the batch export thread


def test_setup_tracing_honours_otel_service_name(
    fresh_setup: list[object], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:9")
    monkeypatch.setenv("OTEL_SERVICE_NAME", "sluice-api")
    tracing.setup_tracing()
    try:
        (provider,) = fresh_setup
        resource = provider.resource  # type: ignore[attr-defined]
        assert resource.attributes["service.name"] == "sluice-api"
    finally:
        for p in fresh_setup:
            p.shutdown()  # type: ignore[attr-defined]


def test_span_error_status_names_the_exception_type_only(spans: InMemorySpanExporter) -> None:
    with pytest.raises(ValueError), tracing.span("sluice.test"):
        raise ValueError(f"secret detail {API_KEY}")
    (s,) = _named(spans, "sluice.test")
    assert s.status.status_code is StatusCode.ERROR
    assert s.status.description == "ValueError"
    assert s.events == ()  # no exception event: its message could carry a secret


# --------------------------------------------------------------------------- LLM spans


class Answer(BaseModel):
    column: str


def _llm(respond: Any) -> LLM:
    return LLM("http://litellm.test", API_KEY, model_override=FunctionModel(respond))


def test_llm_call_span_has_genai_attributes_and_no_prompt(spans: InMemorySpanExporter) -> None:
    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return ModelResponse(
            parts=[output_part(info, json.dumps({"column": "a"}))],
            usage=RequestUsage(input_tokens=11, output_tokens=7),
            model_name="groq/gpt-oss-20b",
        )

    async def go() -> Any:
        async with _llm(respond) as llm:
            return await llm.run("mapper", "small", "SYSTEM-TEXT", "PROMPT-TEXT", Answer)

    _, record = asyncio.run(go())

    (s,) = _named(spans, "chat small")
    attrs = dict(s.attributes or {})
    assert attrs["gen_ai.operation.name"] == "chat"
    assert attrs["gen_ai.system"] == "litellm"
    assert attrs["gen_ai.request.model"] == "small"
    assert attrs["gen_ai.response.model"] == record.model
    assert attrs["gen_ai.usage.input_tokens"] == 11
    assert attrs["gen_ai.usage.output_tokens"] == 7
    assert attrs["sluice.role"] == "mapper"
    assert attrs["sluice.cost_usd"] == pytest.approx(record.cost_usd)
    assert s.kind is trace.SpanKind.CLIENT
    values = _all_values(spans)
    assert not any("SYSTEM-TEXT" in v or "PROMPT-TEXT" in v or API_KEY in v for v in values)


def test_llm_unavailable_span_is_an_error_without_provider_text(
    spans: InMemorySpanExporter,
) -> None:
    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        raise ModelHTTPError(503, "m", body={"error": f"bad key {API_KEY}"})

    async def go() -> None:
        async with _llm(respond) as llm:
            await llm.run("planner", "strong", "s", "p", Answer)

    with pytest.raises(LLMUnavailable):
        asyncio.run(go())

    (s,) = _named(spans, "chat strong")
    assert s.status.status_code is StatusCode.ERROR
    assert s.status.description == "LLMUnavailable"
    assert not any(API_KEY in v for v in _all_values(spans))


# --------------------------------------------------------------------------- sandbox spans


def _write_values(out_dir: Path, series: pl.Series) -> None:
    pl.DataFrame({"values": series}).write_parquet(
        out_dir / runner.VALUES_FILE, compression="uncompressed"
    )


def _strip_commas(values: pl.Series) -> pl.Series:
    return values.str.replace_all(",", "")


@dataclass
class FakeContainer:
    """Plays the sandbox: `wait` runs `play` on the mounts (code is never executed)."""

    kwargs: dict[str, Any]
    wait_exc: BaseException | None = None
    attrs: dict[str, Any] = field(default_factory=lambda: {"State": {"OOMKilled": False}})

    def _mount(self, bind: str) -> Path:
        return next(Path(k) for k, v in self.kwargs["volumes"].items() if v["bind"] == bind)

    def wait(self, timeout: float | None = None) -> dict[str, Any]:
        if self.wait_exc is not None:
            raise self.wait_exc
        in_dir, out_dir = self._mount(runner.IN_MOUNT), self._mount(runner.OUT_MOUNT)
        code = (in_dir / runner.CODE_FILE).read_text(encoding="utf-8")
        values = pl.read_parquet(in_dir / runner.VALUES_FILE)["values"]
        if CRASH in code:
            (out_dir / runner.ERROR_FILE).write_text("ValueError: boom", encoding="utf-8")
            return {"StatusCode": 1}
        _write_values(out_dir, _strip_commas(values))
        return {"StatusCode": 0}

    def kill(self) -> None:
        pass

    def reload(self) -> None:
        pass

    def logs(self, **kwargs: Any) -> bytes:
        return b""

    def remove(self, **kwargs: Any) -> None:
        pass


@dataclass
class FakeDocker:
    wait_exc: BaseException | None = None

    @property
    def containers(self) -> FakeDocker:
        return self

    def run(self, image: str, **kwargs: Any) -> FakeContainer:
        return FakeContainer(kwargs, self.wait_exc)

    def get(self, name: str) -> FakeContainer:
        raise docker.errors.NotFound("gone")


@pytest.fixture
def sandbox_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(runner.WORK_DIR_ENV, str(tmp_path / "sandbox-base"))
    monkeypatch.setattr(runner, "POLL_INTERVAL_S", 0.01)


VALUES = pl.Series("values", ["1,200.50", "7"], dtype=pl.String)


@pytest.mark.usefixtures("sandbox_dir")
def test_sandbox_span_records_exit_code_and_duration(spans: InMemorySpanExporter) -> None:
    code = "def transform(values):\n    return values\n"
    result = runner.run_transform(code, VALUES, client=FakeDocker())  # type: ignore[arg-type]

    assert result.ok
    (s,) = _named(spans, "sluice.sandbox")
    attrs = dict(s.attributes or {})
    assert attrs["sluice.exit_code"] == 0
    assert attrs["sluice.timed_out"] is False
    assert attrs["sluice.duration_ms"] == result.duration_ms
    assert not any(code in v or "1,200.50" in v for v in _all_values(spans))


@pytest.mark.usefixtures("sandbox_dir")
def test_sandbox_span_marks_a_timeout(spans: InMemorySpanExporter) -> None:
    client = FakeDocker(wait_exc=requests.exceptions.ReadTimeout())
    result = runner.run_transform("x", VALUES, timeout_s=0, client=client)  # type: ignore[arg-type]

    assert not result.ok
    (s,) = _named(spans, "sluice.sandbox")
    assert (s.attributes or {})["sluice.timed_out"] is True
    assert (s.attributes or {})["sluice.exit_code"] == -1


# --------------------------------------------------------------------------- pipeline tree


def _module(body: str) -> str:
    return f"import polars as pl\n\n\ndef transform(values: pl.Series) -> pl.Series:\n{body}\n"


CRASH = _module("    raise ValueError('boom')")
STRIP = _module("    return values.str.replace_all(',', '')")
_ROLE_BY_PROMPT = {
    MAPPER_PROMPT: "mapper",
    PLANNER_PROMPT: "orchestrator",
    WORKER_SYSTEM_PROMPT: "worker",
    VERIFIER_SYSTEM_PROMPT: "verifier",
}


@dataclass
class Script:
    """Mapper renames `amt`; one node on the price column fails once (crash), then passes."""

    workers: list[str] = field(default_factory=lambda: [CRASH, STRIP])
    prompts: list[str] = field(default_factory=list)

    def respond(self, messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        system = user = ""
        for m in messages:
            if isinstance(m, ModelRequest):
                for part in m.parts:
                    if isinstance(part, SystemPromptPart) and not system:
                        system = part.content
                    elif isinstance(part, UserPromptPart) and not user:
                        user = str(part.content)
        self.prompts.append(user)
        role = _ROLE_BY_PROMPT[system]
        if role == "mapper":
            out: dict[str, Any] = {
                "mappings": [{"source": "amt", "target": "o_totalprice", "reason": "price"}]
            }
        elif role == "orchestrator":
            out = {
                "nodes": [
                    {
                        "id": "n1",
                        "column": "o_totalprice",
                        "issue": "o_totalprice needs a reformat",
                        "difficulty": "easy",
                        "depends_on": [],
                    }
                ]
            }
        elif role == "worker":
            assert re.search(r'column "o_totalprice"', user)
            out = {"code": self.workers.pop(0), "explanation": "fix"}
        else:
            out = {"accept": True, "reason": "pairs look right"}
        return ModelResponse(
            parts=[output_part(info, json.dumps(out))],
            usage=RequestUsage(input_tokens=100, output_tokens=50),
            model_name="script-model",
        )


@pytest.fixture(scope="module")
def priced_case(tmp_path_factory: pytest.TempPathFactory, orders_df: pl.DataFrame) -> Case:
    ops: list[Any] = [
        ("rename", {"column": "o_totalprice", "new_name": "amt"}),
        ("indian_number_string", {"column": "o_totalprice"}),
    ]
    recipe = Recipe("orders", "medium", ops, "csv", "utf-8", 40)
    out = tmp_path_factory.mktemp("traced-case")
    return build_case("dev-0001", "dev", recipe, {"orders": orders_df}, 7, out)


@dataclass
class Traced:
    events: list[EventModel]
    llm: LLM
    script: Script
    outcome: RunOutcome


PUBLIC_RUN_ID = "run-public-7"


def _fake_load(out: SystemOutput, contract: Any, **kw: Any) -> LoadResult:
    return LoadResult(
        snapshot_id="42",
        rows_loaded=out.df.height,
        rows_quarantined=len(out.quarantined),
        skipped=False,
    )


def _execute(path: Path, run_fn: Any, events: list[EventModel]) -> RunOutcome:
    return execute_run(
        path,
        "orders",
        run_id=PUBLIC_RUN_ID,
        emit=events.append,
        run_fn=run_fn,
        load_fn=_fake_load,
        loaded_fn=lambda *_a, **_k: None,
        settings_fn=lambda: object(),  # type: ignore[arg-type, return-value]
        reference_fn=lambda _t: {},
    )


@pytest.fixture
def traced_run(priced_case: Case, spans: InMemorySpanExporter, sandbox_dir: None) -> Traced:
    """One agent run through the shared execution core, on the real runner (fake Docker)."""
    script = Script()
    llms: list[LLM] = []

    def sandbox(code: str, values: pl.Series) -> runner.SandboxResult:
        return runner.run_transform(code, values, client=FakeDocker())  # type: ignore[arg-type]

    def factory() -> LLM:
        llms.append(_llm(script.respond))
        return llms[-1]

    events: list[EventModel] = []
    system = AgentSystem(llm_factory=factory, sandbox=sandbox)
    outcome = _execute(priced_case.input_path, system.run, events)
    (llm,) = llms
    return Traced(events, llm, script, outcome)


def _ancestors(s: ReadableSpan, by_id: dict[int, ReadableSpan]) -> list[str]:
    names = []
    parent = s.parent
    while parent is not None:
        p = by_id[parent.span_id]
        names.append(p.name)
        parent = p.parent
    return names


def test_pipeline_run_is_one_root_span_with_run_attributes(
    traced_run: Traced, spans: InMemorySpanExporter
) -> None:
    (root,) = [s for s in spans.get_finished_spans() if s.parent is None]
    assert root.name == "sluice.run"
    (started,) = [e for e in traced_run.events if e.type == "run_started"]  # type: ignore[attr-defined]
    attrs = dict(root.attributes or {})
    assert attrs["sluice.run_id"] == PUBLIC_RUN_ID == started.run_id  # type: ignore[attr-defined]
    assert attrs["sluice.table"] == "orders"
    assert attrs["sluice.status"] == traced_run.outcome.status == "succeeded"
    assert {s.context.trace_id for s in spans.get_finished_spans()} == {root.context.trace_id}


def test_pipeline_steps_are_descendants_of_the_run(
    traced_run: Traced, spans: InMemorySpanExporter
) -> None:
    by_id = {s.context.span_id: s for s in spans.get_finished_spans()}
    steps = _named(spans, "sluice.step")
    assert sorted(str((s.attributes or {})["sluice.step"]) for s in steps) == sorted(
        ["mapping", "profile", "rules_pass_1", "plan", "rules_pass_2", "load"]
    )
    assert all(_ancestors(s, by_id) == ["sluice.run"] for s in steps)


def test_pipeline_one_chat_span_per_call_record(
    traced_run: Traced, spans: InMemorySpanExporter
) -> None:
    chats = [s for s in spans.get_finished_spans() if s.name.startswith("chat ")]
    records = traced_run.llm.records
    assert len(chats) == len(records) == 5  # mapper, planner, worker x2, verifier

    def key(attrs: Any) -> tuple[Any, ...]:
        return (
            attrs["sluice.role"],
            attrs["gen_ai.request.model"],
            attrs["gen_ai.usage.input_tokens"],
            attrs["gen_ai.usage.output_tokens"],
            attrs["gen_ai.response.model"],
        )

    got = sorted(key(dict(s.attributes or {})) for s in chats)
    want = sorted((r.role, r.tier, r.input_tokens, r.output_tokens, r.model) for r in records)
    assert got == want
    assert all(s.name == f"chat {(s.attributes or {})['gen_ai.request.model']}" for s in chats)


def test_pipeline_node_attempts_nest_their_calls_and_sandbox_runs(
    traced_run: Traced, spans: InMemorySpanExporter
) -> None:
    by_id = {s.context.span_id: s for s in spans.get_finished_spans()}
    attempts = sorted(
        _named(spans, "sluice.node.attempt"), key=lambda s: (s.attributes or {})["sluice.attempt"]
    )
    assert [
        (
            a["sluice.node_id"],
            a["sluice.attempt"],
            a["sluice.tier"],
            a["sluice.outcome"],
        )
        for a in (dict(s.attributes or {}) for s in attempts)
    ] == [("n1", 1, "small", "failed"), ("n1", 2, "strong", "accepted")]
    assert all(_ancestors(s, by_id) == ["sluice.run"] for s in attempts)

    sandboxes = _named(spans, "sluice.sandbox")
    assert len(sandboxes) == len(attempts)  # each attempt reached the sandbox once
    assert sorted((s.attributes or {})["sluice.exit_code"] for s in sandboxes) == [0, 1]
    for s in sandboxes:  # the worker thread kept the attempt's context
        assert _ancestors(s, by_id) == ["sluice.node.attempt", "sluice.run"]
    worker_chats = [
        s
        for s in spans.get_finished_spans()
        if (s.attributes or {}).get("sluice.role") in ("worker", "verifier")
    ]
    assert worker_chats
    assert all(_ancestors(s, by_id)[0] == "sluice.node.attempt" for s in worker_chats)


def test_pipeline_spans_carry_no_secret_prompt_or_code(
    traced_run: Traced, spans: InMemorySpanExporter
) -> None:
    values = _all_values(spans)
    needles = [API_KEY, MAPPER_PROMPT, PLANNER_PROMPT, WORKER_SYSTEM_PROMPT, CRASH, STRIP]
    needles += [p for p in traced_run.script.prompts if p]
    assert not [n for n in needles if any(n in v for v in values)]


# --------------------------------------------------------------------------- entry points


def test_create_app_and_cli_entry_point_set_up_tracing(monkeypatch: pytest.MonkeyPatch) -> None:
    from sluice import cli
    from sluice.api import app as api_app

    calls: list[str] = []
    monkeypatch.setattr(tracing, "setup_tracing", lambda *a, **k: calls.append("setup"))

    api_app.create_app(worker=False)
    assert calls == ["setup"]
    cli.main()
    assert calls == ["setup", "setup"]


def test_phoenix_service_is_opt_in_pinned_and_loopback_only() -> None:
    import yaml

    compose = yaml.safe_load((Path(__file__).resolve().parents[2] / "compose.yaml").read_text())
    phoenix = compose["services"]["phoenix"]
    assert phoenix["profiles"] == ["observability"]  # `make up` does not start it
    assert re.fullmatch(
        r"arizephoenix/phoenix:version-[\d.]+@sha256:[0-9a-f]{64}", phoenix["image"]
    )
    assert phoenix["ports"] == ["127.0.0.1:56006:6006"]  # UI + OTLP/HTTP share 6006
    others = [s for name, s in compose["services"].items() if name != "phoenix"]
    assert all("profiles" not in s for s in others)


def test_execute_run_failure_marks_the_run_span_failed(
    priced_case: Case, spans: InMemorySpanExporter
) -> None:
    def boom(case: CaseInput) -> SystemOutput:
        raise RuntimeError(f"system crashed {API_KEY}")

    with pytest.raises(RuntimeError):
        _execute(priced_case.input_path, boom, [])

    (root,) = _named(spans, "sluice.run")
    assert (root.attributes or {})["sluice.run_id"] == PUBLIC_RUN_ID
    assert (root.attributes or {})["sluice.status"] == "failed"
    assert root.status.status_code is StatusCode.ERROR
    assert root.status.description == "RuntimeError"
    assert not any(API_KEY in v for v in _all_values(spans))


def test_llm_output_error_span_keeps_the_spent_tokens(spans: InMemorySpanExporter) -> None:
    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return ModelResponse(
            parts=[output_part(info, json.dumps({"wrong": API_KEY}))],
            usage=RequestUsage(input_tokens=5, output_tokens=3),
            model_name="m",
        )

    async def go() -> None:
        async with _llm(respond) as llm:
            await llm.run("worker", "small", "s", "p", Answer)

    with pytest.raises(LLMOutputError) as caught:
        asyncio.run(go())

    (s,) = _named(spans, "chat small")
    attrs = dict(s.attributes or {})
    assert s.status.status_code is StatusCode.ERROR
    assert s.status.description == "LLMOutputError"
    assert attrs["gen_ai.usage.input_tokens"] == caught.value.record.input_tokens > 0
    assert attrs["gen_ai.usage.output_tokens"] == caught.value.record.output_tokens > 0
    assert not any(API_KEY in v for v in _all_values(spans))
