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


def _run(recorder: _Recorder, path: Path, table: str = "orders") -> LoadResult:
    return cli.run_file(
        path,
        table,
        run_fn=recorder.run_fn,
        load_fn=recorder.load_fn,
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
    assert events[3]["status"] == "succeeded"
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
