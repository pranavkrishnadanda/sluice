"""The shared execution core: live events, one run id end to end, CLI-identical semantics.

No services: the agent pipeline is scripted (`pipeline_fakes`), loads are recorded fakes.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import polars as pl
import pytest
from pipeline_fakes import CRASH, STRIP_COMMAS, FakeSandbox, Script, node
from pydantic_ai.models.function import FunctionModel

from sluice.agent.events import EventModel
from sluice.agent.pipeline import transform_id
from sluice.contracts.model import Contract
from sluice.drift.cases import Case, Recipe, build_case
from sluice.evals.types import CaseInput, SystemOutput
from sluice.llm.client import LLM
from sluice.load.iceberg import LoadResult
from sluice.runs.execute import RunOutcome, execute_run
from sluice.systems.agent import AgentSystem
from sluice.systems.rules_only import RulesOnlySystem

INDIAN = ("indian_number_string", {"column": "o_totalprice"})
PRICE_PLAN = {"nodes": [node("n1", "o_totalprice")]}


@pytest.fixture(scope="module")
def indian(tmp_path_factory: pytest.TempPathFactory, orders_df: pl.DataFrame) -> Case:
    recipe = Recipe("orders", "medium", [INDIAN], "csv", "utf-8", 40)
    return build_case(
        "dev-0101", "dev", recipe, {"orders": orders_df}, 7, tmp_path_factory.mktemp("x")
    )


class Loads:
    """A recording ``load_fn`` that writes a snapshot (or skips, when told to)."""

    def __init__(self, *, skipped: bool = False, snapshot: str | None = "42") -> None:
        self.calls: list[dict[str, Any]] = []
        self.skipped, self.snapshot = skipped, snapshot

    def __call__(self, out: SystemOutput, contract: Contract, **kw: Any) -> LoadResult:
        self.calls.append({"out": out, **kw})
        return LoadResult(
            snapshot_id=self.snapshot,
            rows_loaded=out.df.height,
            rows_quarantined=len(out.quarantined),
            skipped=self.skipped,
        )


def _agent(script: Script, sandbox: FakeSandbox) -> AgentSystem:
    def factory() -> LLM:
        return LLM("http://litellm.test", "k", model_override=FunctionModel(script.respond))

    return AgentSystem(llm_factory=factory, sandbox=sandbox)


def _execute(
    path: Path, run_fn: Any, emit: Any, *, loads: Loads | None = None, run_id: str = "run-1"
) -> RunOutcome:
    return execute_run(
        path,
        "orders",
        run_id=run_id,
        emit=emit,
        run_fn=run_fn,
        load_fn=loads or Loads(),
        loaded_fn=lambda *_a, **_k: None,
        settings_fn=lambda: object(),  # type: ignore[arg-type, return-value]
        reference_fn=lambda _t: {},
    )


def _types(events: list[EventModel]) -> list[str]:
    return [e.type for e in events]  # type: ignore[attr-defined]


def test_execute_run_agent_events_are_emitted_live_before_the_sandbox_returns(
    indian: Case,
) -> None:
    emitted: list[EventModel] = []
    seen_in_sandbox: list[list[str]] = []
    sandbox = FakeSandbox(on_call=lambda: seen_in_sandbox.append(_types(emitted)))
    script = Script(plan=PRICE_PLAN, workers={"o_totalprice": [STRIP_COMMAS]})

    _execute(indian.input_path, _agent(script, sandbox).run, emitted.append)

    (during,) = seen_in_sandbox
    assert during[-1] == "node_started"  # live, not replayed after the pipeline returned
    assert during[:4] == ["run_started", "profiled", "rules_applied", "plan_created"]


def test_execute_run_events_are_one_gapless_sequence_with_one_start_and_finish(
    indian: Case,
) -> None:
    emitted: list[EventModel] = []
    script = Script(plan=PRICE_PLAN, workers={"o_totalprice": [CRASH, STRIP_COMMAS]})

    outcome = _execute(indian.input_path, _agent(script, FakeSandbox()).run, emitted.append)

    assert [e.seq for e in emitted] == list(range(1, len(emitted) + 1))
    assert _types(emitted) == [
        "run_started",
        "profiled",
        "rules_applied",
        "plan_created",
        "node_started",
        "attempt_failed",
        "node_started",
        "node_finished",
        "verified",
        "loaded",
        "run_finished",
    ]
    assert all(e.at for e in emitted)
    assert outcome.status == "succeeded"
    assert outcome.output is not None and outcome.result is not None
    assert outcome.tokens == emitted[-1].tokens  # type: ignore[attr-defined]
    assert outcome.cost_usd == emitted[-1].cost_usd  # type: ignore[attr-defined]


def test_execute_run_run_started_carries_run_id_and_row_count(indian: Case) -> None:
    emitted: list[EventModel] = []
    _execute(indian.input_path, RulesOnlySystem().run, emitted.append, run_id="abc_123")
    started = emitted[0]
    assert started.type == "run_started"  # type: ignore[attr-defined]
    assert started.run_id == "abc_123"  # type: ignore[attr-defined]
    assert started.rows == 40  # type: ignore[attr-defined]
    assert started.file_name == indian.input_path.name  # type: ignore[attr-defined]


def test_execute_run_unreadable_file_starts_with_null_rows(tmp_path: Path) -> None:
    path = tmp_path / "broken.json"
    path.write_text("{not json", encoding="utf-8")
    emitted: list[EventModel] = []
    with pytest.raises(ValueError):
        _execute(path, RulesOnlySystem().run, emitted.append)
    assert emitted[0].rows is None  # type: ignore[attr-defined]
    assert _types(emitted) == ["run_started", "run_finished"]
    assert emitted[-1].status == "failed"  # type: ignore[attr-defined]


def test_execute_run_decompression_bomb_fails_before_any_full_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ruling F3p4: the worker's run refuses a Parquet whose footer declares too much data."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    from sluice import io as sluice_io
    from sluice.runs.execute import DECODED_TOO_LARGE_SUMMARY

    path = tmp_path / "bomb.parquet"
    pq.write_table(pa.table({"o_orderkey": pa.nulls(sluice_io.MAX_PARQUET_ROWS + 1)}), path)

    def refuse(*_a: object, **_k: object) -> None:
        raise AssertionError("no full read of a refused file")

    monkeypatch.setattr(sluice_io.pl, "read_parquet", refuse)
    ran: list[object] = []
    loads = Loads()
    emitted: list[EventModel] = []
    with pytest.raises(sluice_io.DecodedTooLarge):
        _execute(path, ran.append, emitted.append, loads=loads)
    assert ran == [] and loads.calls == []
    assert _types(emitted) == ["run_started", "run_finished"]
    assert emitted[0].rows is None  # type: ignore[attr-defined]
    assert emitted[-1].status == "failed"  # type: ignore[attr-defined]
    assert emitted[-1].summary == DECODED_TOO_LARGE_SUMMARY  # type: ignore[attr-defined]


def test_execute_run_one_run_id_end_to_end(indian: Case) -> None:
    emitted: list[EventModel] = []
    loads = Loads()
    script = Script(plan=PRICE_PLAN, workers={"o_totalprice": [CRASH, STRIP_COMMAS]})
    run_id = uuid.uuid4().hex

    outcome = _execute(
        indian.input_path, _agent(script, FakeSandbox()).run, emitted.append, loads=loads,
        run_id=run_id,
    )  # fmt: skip

    assert outcome.output is not None
    meta = outcome.output.meta
    assert meta["run_id"] == run_id
    assert loads.calls[0]["run_id"] == run_id
    (done,) = [e for e in emitted if e.type == "node_finished"]  # type: ignore[attr-defined]
    assert done.transform_id == transform_id(run_id, "n1", 2)  # type: ignore[attr-defined]
    ids = [t["transform_id"] for t in meta["transforms"]]
    assert ids == [transform_id(run_id, "n1", 1), transform_id(run_id, "n1", 2)]
    assert done.transform_id in ids  # type: ignore[attr-defined]


def test_execute_run_skipped_load_emits_no_loaded(indian: Case) -> None:
    emitted: list[EventModel] = []
    loads = Loads(skipped=True, snapshot="7")
    outcome = _execute(indian.input_path, RulesOnlySystem().run, emitted.append, loads=loads)
    assert "loaded" not in _types(emitted)
    assert _types(emitted).count("run_finished") == 1
    assert outcome.status == "succeeded"
    assert outcome.result is not None and outcome.result.skipped


def test_execute_run_failed_pipeline_returns_failed_without_loading(indian: Case) -> None:
    emitted: list[EventModel] = []
    loads = Loads()
    script = Script(plan=PRICE_PLAN, workers={"o_totalprice": [STRIP_COMMAS]}, down={"worker"})

    outcome = _execute(
        indian.input_path, _agent(script, FakeSandbox()).run, emitted.append, loads=loads
    )

    assert outcome.status == "failed"
    assert outcome.result is None
    assert outcome.output is not None
    assert loads.calls == []
    assert "loaded" not in _types(emitted)
    assert _types(emitted).count("run_finished") == 1
    assert emitted[-1].status == "failed"  # type: ignore[attr-defined]


def test_execute_run_rules_system_is_replayed_from_meta(indian: Case) -> None:
    emitted: list[EventModel] = []
    outcome = _execute(indian.input_path, RulesOnlySystem().run, emitted.append)
    assert _types(emitted) == ["run_started", "rules_applied", "loaded", "run_finished"]
    assert outcome.status in ("succeeded", "quarantined")


def test_execute_run_already_loaded_skips_the_pipeline(indian: Case) -> None:
    emitted: list[EventModel] = []
    prior = LoadResult(snapshot_id="9", rows_loaded=40, rows_quarantined=0, skipped=True)

    def must_not_run(case: CaseInput) -> SystemOutput:
        pytest.fail("the pipeline must not run for an already-loaded file")

    outcome = execute_run(
        indian.input_path,
        "orders",
        run_id="run-1",
        emit=emitted.append,
        run_fn=must_not_run,
        load_fn=Loads(),
        loaded_fn=lambda *_a, **_k: prior,
        settings_fn=lambda: object(),  # type: ignore[arg-type, return-value]
        reference_fn=lambda _t: {},
    )
    assert outcome == RunOutcome(
        result=prior, status="succeeded", output=None, cost_usd=0.0, tokens=0
    )
    assert _types(emitted) == ["run_started", "run_finished"]


def test_execute_run_hands_on_transform_to_the_system(indian: Case) -> None:
    seen: list[Any] = []

    def hook(_record: dict[str, Any]) -> None:
        return None

    def system(case: CaseInput) -> SystemOutput:
        seen.append(case.on_transform)
        return RulesOnlySystem().run(case)

    execute_run(
        indian.input_path,
        "orders",
        run_id="run-1",
        emit=lambda _e: None,
        on_transform=hook,
        run_fn=system,
        load_fn=Loads(),
        loaded_fn=lambda *_a, **_k: None,
        settings_fn=lambda: object(),  # type: ignore[arg-type, return-value]
        reference_fn=lambda _t: {},
    )
    assert seen == [hook]


def test_execute_run_without_on_transform_gives_the_system_none(indian: Case) -> None:
    seen: list[Any] = []

    def system(case: CaseInput) -> SystemOutput:
        seen.append(case.on_transform)
        return RulesOnlySystem().run(case)

    _execute(indian.input_path, system, lambda _e: None)
    assert seen == [None]


# --- round 2: a whole-file quarantine summary says "nothing loaded" once --------------------


def _quarantined_whole(summary: str) -> Any:
    from sluice.agent.events import EventLog, RunFinished

    final = RunFinished(status="quarantined", summary=summary, cost_usd=0.0, tokens=0)

    def run_fn(case: CaseInput) -> SystemOutput:
        return SystemOutput(
            df=pl.DataFrame(schema=case.contract.polars_schema()),
            flags=set(),
            quarantined=set(),
            file_quarantined=True,
            cost_usd=0.0,
            latency_s=0.0,
            meta={"events": [EventLog().emit(final).to_dict()], "tokens": 0},
        )

    return run_fn


def test_execute_run_whole_file_quarantine_summary_says_nothing_loaded_once(
    indian: Case,
) -> None:
    emitted: list[EventModel] = []
    mismatch = "this looks like a lineitem file, not orders; nothing loaded"

    _execute(
        indian.input_path, _quarantined_whole(mismatch), emitted.append, loads=Loads(snapshot=None)
    )

    summary = emitted[-1].summary  # type: ignore[attr-defined]
    assert summary == f"{mismatch}; the file can be retried"


def test_execute_run_whole_file_quarantine_summary_keeps_its_own_reason(indian: Case) -> None:
    emitted: list[EventModel] = []

    _execute(
        indian.input_path,
        _quarantined_whole("file quarantined: node n1 failed"),
        emitted.append,
        loads=Loads(snapshot=None),
    )

    summary = emitted[-1].summary  # type: ignore[attr-defined]
    assert summary == "file quarantined: node n1 failed; nothing loaded; the file can be retried"
