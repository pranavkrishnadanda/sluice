"""Unit tests (no services) for `sluice run`: run_fn resolution, event lines, load hand-off."""

import dataclasses
import hashlib
import json
from pathlib import Path
from typing import Any

import polars as pl
import pytest
from typer.testing import CliRunner

from sluice import cli
from sluice.contracts.model import Contract, load_contract
from sluice.evals.types import CaseInput, SystemOutput
from sluice.load.iceberg import LoadResult


class _Recorder:
    def __init__(self) -> None:
        self.cases: list[CaseInput] = []
        self.loads: list[dict[str, Any]] = []
        self.lines: list[str] = []

    def run_fn(self, case: CaseInput) -> SystemOutput:
        self.cases.append(case)
        return SystemOutput(
            df=pl.DataFrame(schema=case.contract.polars_schema()),
            flags=set(),
            quarantined={(9,)},
            file_quarantined=False,
            cost_usd=0.25,
            latency_s=0.1,
            meta={"actions": ["trimmed o_comment"]},
        )

    def load_fn(self, out: SystemOutput, contract: Contract, **kwargs: Any) -> LoadResult:
        self.loads.append({"out": out, "contract": contract, **kwargs})
        return LoadResult(snapshot_id="123", rows_loaded=0, rows_quarantined=1, skipped=False)


@pytest.fixture
def input_file(tmp_path: Path) -> Path:
    path = tmp_path / "orders.csv"
    path.write_bytes(b"o_orderkey\n1\n")
    return path


def _run(
    recorder: _Recorder,
    path: Path,
    table: str = "orders",
    loaded: LoadResult | None = None,
) -> LoadResult:
    return cli.run_file(
        path,
        table,
        run_fn=recorder.run_fn,
        load_fn=recorder.load_fn,
        loaded_fn=lambda *_a, **_k: loaded,
        settings_fn=lambda: object(),  # type: ignore[arg-type, return-value]
        reference_fn=lambda _t: {},
        echo=recorder.lines.append,
    )


def test_run_file_passes_contract_and_reference_to_run_fn(input_file: Path) -> None:
    rec = _Recorder()
    _run(rec, input_file)
    assert rec.cases[0].path == input_file
    assert rec.cases[0].contract == load_contract("orders")
    assert rec.cases[0].reference == {}


def test_run_file_loads_with_file_sha256_and_run_id(input_file: Path) -> None:
    rec = _Recorder()
    _run(rec, input_file)
    load = rec.loads[0]
    assert load["file_sha256"] == hashlib.sha256(input_file.read_bytes()).hexdigest()
    assert load["contract"].name == "orders"
    assert len(load["run_id"]) == 32


def test_run_file_prints_events_as_json_lines(input_file: Path) -> None:
    rec = _Recorder()
    _run(rec, input_file)
    events = [json.loads(line) for line in rec.lines]
    types = [e["type"] for e in events]
    assert types == ["run_started", "rules_applied", "loaded", "run_finished"]
    assert [e["seq"] for e in events] == [1, 2, 3, 4]
    assert events[2]["snapshotId"] == "123"
    assert events[3]["status"] == "quarantined"  # 1 quarantined row (contract "Statuses")
    assert events[3]["costUsd"] == 0.25


def test_run_file_replays_pipeline_events_from_meta(input_file: Path) -> None:
    rec = _Recorder()
    base = rec.run_fn

    def run_fn(case: CaseInput) -> SystemOutput:
        out = base(case)
        event = {"type": "profiled", "issues": [], "seq": 1, "at": "2026-10-28T00:00:00.000Z"}
        return dataclasses.replace(out, meta={"events": [event]})

    rec.run_fn = run_fn  # type: ignore[method-assign]
    _run(rec, input_file)
    types = [json.loads(line)["type"] for line in rec.lines]
    assert types == ["run_started", "profiled", "loaded", "run_finished"]


def test_run_file_skipped_load_reports_skip_without_loaded_event(input_file: Path) -> None:
    rec = _Recorder()
    rec.load_fn = lambda out, contract, **kw: LoadResult(  # type: ignore[method-assign]
        snapshot_id=None, rows_loaded=0, rows_quarantined=0, skipped=True
    )
    result = _run(rec, input_file)
    events = [json.loads(line) for line in rec.lines]
    assert result.skipped is True
    assert "loaded" not in [e["type"] for e in events]
    assert "already loaded" in events[-1]["summary"]


def test_run_file_file_quarantined_status(input_file: Path) -> None:
    rec = _Recorder()
    base = rec.run_fn

    def run_fn(case: CaseInput) -> SystemOutput:
        out = base(case)
        return dataclasses.replace(out, file_quarantined=True)

    rec.run_fn = run_fn  # type: ignore[method-assign]
    _run(rec, input_file)
    assert json.loads(rec.lines[-1])["status"] == "quarantined"


def test_resolve_run_fn_falls_back_to_rules_with_note(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "SYSTEMS", {"rules": cli.SYSTEMS["rules"]})
    notes: list[str] = []
    fn = cli.resolve_run_fn(note=notes.append)
    assert callable(fn)
    assert len(notes) == 1
    assert "agent" in notes[0] and "rules" in notes[0]


def test_resolve_run_fn_prefers_agent_when_registered(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Agent:
        name = "agent"

        def __init__(self, *, cache: bool = False) -> None:
            self.cache = cache

        def run(self, case: CaseInput) -> SystemOutput:
            raise NotImplementedError

    monkeypatch.setattr(cli, "SYSTEMS", {"rules": cli.SYSTEMS["rules"], "agent": _Agent})
    notes: list[str] = []
    fn = cli.resolve_run_fn(note=notes.append)
    assert notes == []
    assert getattr(fn, "__self__", None).__class__ is _Agent


def test_run_command_rejects_unknown_table(input_file: Path) -> None:
    result = CliRunner().invoke(cli.app, ["run", str(input_file), "--table", "customers"])
    assert result.exit_code != 0


def test_run_command_missing_file_exits_nonzero(tmp_path: Path) -> None:
    result = CliRunner().invoke(cli.app, ["run", str(tmp_path / "nope.csv"), "--table", "orders"])
    assert result.exit_code != 0


# --- fix round 1: pre-check skips the pipeline; clean DB error lines ------------------------


def test_run_file_already_loaded_skips_pipeline_and_load(input_file: Path) -> None:
    rec = _Recorder()

    def must_not_run(case: CaseInput) -> SystemOutput:
        pytest.fail("the pipeline must not run for an already-loaded file")

    rec.run_fn = must_not_run  # type: ignore[method-assign]
    prior = LoadResult(snapshot_id="77", rows_loaded=3, rows_quarantined=0, skipped=True)
    result = _run(rec, input_file, loaded=prior)
    events = [json.loads(line) for line in rec.lines]
    assert result.skipped is True
    assert rec.loads == []
    assert [e["type"] for e in events] == ["run_started", "run_finished"]
    assert "already loaded" in events[-1]["summary"]
    assert events[-1]["costUsd"] == 0.0


def test_run_file_precheck_receives_table_key(input_file: Path) -> None:
    rec = _Recorder()
    seen: list[tuple[Any, ...]] = []

    def loaded_fn(contract: Contract, sha: str, **kw: Any) -> None:
        seen.append((contract.name, sha, kw.get("settings")))

    cli.run_file(
        input_file,
        "orders",
        run_fn=rec.run_fn,
        load_fn=rec.load_fn,
        loaded_fn=loaded_fn,
        settings_fn=lambda: "S",  # type: ignore[arg-type, return-value]
        reference_fn=lambda _t: {},
        echo=rec.lines.append,
    )
    assert seen == [("orders", hashlib.sha256(input_file.read_bytes()).hexdigest(), "S")]
    assert len(rec.loads) == 1


@pytest.mark.parametrize("make_exc", ["psycopg", "sqlalchemy"])
def test_run_command_db_error_prints_clean_line_without_details(
    input_file: Path, monkeypatch: pytest.MonkeyPatch, make_exc: str
) -> None:
    import psycopg
    import sqlalchemy.exc

    secret = "postgresql://sluice:hunter2@localhost:55432/sluice"
    if make_exc == "psycopg":
        exc: Exception = psycopg.OperationalError(f"connection failed: {secret}")
        name = "OperationalError"
    else:
        exc = sqlalchemy.exc.OperationalError(secret, {}, Exception(secret))
        name = "OperationalError"

    def boom(*_a: object, **_k: object) -> LoadResult:
        raise exc

    monkeypatch.setattr(cli, "run_file", boom)
    result = CliRunner().invoke(cli.app, ["run", str(input_file), "--table", "orders"])
    assert result.exit_code == 1
    assert f"run failed: {name}" in result.output
    assert "hunter2" not in result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)


# --- final review C-1 / Ruling F1: the pipeline's own run_finished drives the CLI ------------


def _finished(status: str, summary: str, *, tokens: int = 300, cost: float = 0.5) -> dict[str, Any]:
    from sluice.agent.events import EventLog, RunFinished

    event = RunFinished(status=status, summary=summary, cost_usd=cost, tokens=tokens)  # type: ignore[arg-type]
    return EventLog().emit(event).to_dict()


def _pipeline_fn(
    rec: _Recorder,
    status: str,
    summary: str,
    *,
    file_quarantined: bool = False,
    quarantined: set[tuple[Any, ...]] | None = None,
) -> Any:
    def run_fn(case: CaseInput) -> SystemOutput:
        rec.cases.append(case)
        return SystemOutput(
            df=pl.DataFrame(schema=case.contract.polars_schema()),
            flags=set(),
            quarantined=set() if quarantined is None else quarantined,
            file_quarantined=file_quarantined,
            cost_usd=0.5,
            latency_s=0.1,
            meta={"events": [_finished(status, summary)], "tokens": 300},
        )

    return run_fn


def test_run_file_failed_pipeline_does_not_load_and_raises(input_file: Path) -> None:
    rec = _Recorder()
    rec.run_fn = _pipeline_fn(rec, "failed", "stopped by the tokens cap")  # type: ignore[method-assign]
    with pytest.raises(cli.RunFailedError):
        _run(rec, input_file)
    events = [json.loads(line) for line in rec.lines]
    assert rec.loads == []
    assert [e["type"] for e in events] == ["run_started", "run_finished"]
    assert events[-1]["status"] == "failed"
    assert events[-1]["summary"] == "stopped by the tokens cap"
    assert events[-1]["tokens"] == 300
    assert events[-1]["costUsd"] == 0.5


def test_run_file_crashed_pipeline_does_not_load(input_file: Path) -> None:
    rec = _Recorder()
    rec.run_fn = _pipeline_fn(  # type: ignore[method-assign]
        rec, "failed", "the run failed: APIError", file_quarantined=True
    )
    with pytest.raises(cli.RunFailedError):
        _run(rec, input_file)
    events = [json.loads(line) for line in rec.lines]
    assert rec.loads == []
    assert events[-1]["status"] == "failed"
    assert events.count(events[-1]) == 1
    assert [e["type"] for e in events].count("run_finished") == 1


def test_run_file_retry_after_failure_runs_and_loads(input_file: Path) -> None:
    rec = _Recorder()
    good = rec.run_fn
    rec.run_fn = _pipeline_fn(rec, "failed", "sandbox unavailable")  # type: ignore[method-assign]
    with pytest.raises(cli.RunFailedError):
        _run(rec, input_file)
    rec.run_fn = good  # type: ignore[method-assign]
    rec.lines.clear()
    result = _run(rec, input_file)
    assert result.skipped is False
    assert len(rec.loads) == 1


def test_run_file_row_quarantined_status_comes_from_pipeline(input_file: Path) -> None:
    rec = _Recorder()
    rec.run_fn = _pipeline_fn(  # type: ignore[method-assign]
        rec, "quarantined", "1 row(s) quarantined", quarantined={(9,)}
    )
    _run(rec, input_file)
    events = [json.loads(line) for line in rec.lines]
    assert rec.loads[0]["status"] == "quarantined"
    assert events[-1]["status"] == "quarantined"
    assert events[-1]["tokens"] == 300
    assert "1 row(s) quarantined" in events[-1]["summary"]


def test_run_file_rules_output_with_quarantined_rows_is_quarantined(input_file: Path) -> None:
    rec = _Recorder()  # no events in meta (the rules-only system): status from the output
    _run(rec, input_file)
    events = [json.loads(line) for line in rec.lines]
    assert rec.loads[0]["status"] == "quarantined"
    assert events[-1]["status"] == "quarantined"


def test_run_file_whole_file_quarantine_records_run_without_loaded_event(input_file: Path) -> None:
    rec = _Recorder()
    rec.run_fn = _pipeline_fn(  # type: ignore[method-assign]
        rec, "quarantined", "file quarantined: node n1 failed", file_quarantined=True
    )
    rec.load_fn = lambda out, contract, **kw: (  # type: ignore[method-assign]
        rec.loads.append({"out": out, **kw})
        or LoadResult(snapshot_id=None, rows_loaded=0, rows_quarantined=0, skipped=False)
    )
    _run(rec, input_file)
    events = [json.loads(line) for line in rec.lines]
    assert rec.loads[0]["status"] == "quarantined"
    assert "loaded" not in [e["type"] for e in events]
    assert events[-1]["status"] == "quarantined"
    assert "file quarantined: node n1 failed" in events[-1]["summary"]


def test_run_file_exception_reports_tokens_spent(input_file: Path) -> None:
    rec = _Recorder()
    rec.run_fn = _pipeline_fn(rec, "succeeded", "ok")  # type: ignore[method-assign]

    def boom(*_a: object, **_k: object) -> LoadResult:
        raise OSError("disk")

    rec.load_fn = boom  # type: ignore[method-assign, assignment]
    with pytest.raises(OSError):
        _run(rec, input_file)
    last = json.loads(rec.lines[-1])
    assert last["status"] == "failed"
    assert last["tokens"] == 300


def test_run_command_failed_pipeline_exits_one(
    input_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def failed(*_a: object, **_k: object) -> LoadResult:
        raise cli.RunFailedError("stopped by the wall cap")

    monkeypatch.setattr(cli, "run_file", failed)
    result = CliRunner().invoke(cli.app, ["run", str(input_file), "--table", "orders"])
    assert result.exit_code == 1
    assert "stopped by the wall cap" in result.output
