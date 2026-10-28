import json
from dataclasses import fields
from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from sluice.cli import app
from sluice.drift.cases import build_split, load_split, split_hash
from sluice.evals.graders import Grade
from sluice.evals.runner import (
    METRICS,
    SYSTEMS,
    EvalSummary,
    format_ci,
    latest_summaries,
    load_summary,
    render_results,
    run_eval,
    summarize,
)
from sluice.evals.stats import bootstrap_ci
from sluice.systems.rules_only import RulesOnlySystem

FIXED_TIME = datetime(2026, 10, 28, 12, 0, 0, tzinfo=UTC)


@pytest.fixture(scope="module")
def small_cases(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Six dev cases (seed 1), built once for this module. Do not modify."""
    out = tmp_path_factory.mktemp("cases") / "dev"
    build_split("dev", 6, 1, out)
    return out


@pytest.fixture(scope="module")
def flagged_cases(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Ten dev cases (seed 1); at least one expects flagged (unrecoverable) cells."""
    out = tmp_path_factory.mktemp("flagged") / "dev"
    built = build_split("dev", 10, 1, out)
    assert any(c.manifest["expected_flags"] for c in built), "fixture needs a flagged case"
    return out


def _lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


# --- run_eval --------------------------------------------------------------------------------


def test_run_eval_small(small_cases: Path, tmp_path: Path) -> None:
    s = run_eval("rules", "dev", 2, small_cases, tmp_path / "out")
    assert len(_lines(tmp_path / "out" / "results.jsonl")) == 12
    ci = s.metrics["cell_accuracy"]
    assert ci is not None
    mean, lo, hi = ci
    assert lo <= mean <= hi
    assert s.pass_hat_k is not None
    assert set(s.by_difficulty) <= {"easy", "medium", "hard"}
    assert (s.system, s.split, s.trials, s.n_cases) == ("rules", "dev", 2, 6)


def test_run_eval_results_lines_ordered_with_every_grade_field(
    small_cases: Path, tmp_path: Path
) -> None:
    run_eval("rules", "dev", 2, small_cases, tmp_path)
    lines = _lines(tmp_path / "results.jsonl")
    keys = [(r["case_id"], r["trial"]) for r in lines]
    assert keys == sorted(keys)
    assert {r["trial"] for r in lines} == {0, 1}
    expected = {f.name for f in fields(Grade)} | {"trial", "cost_usd", "latency_s"}
    assert all(set(r) == expected for r in lines)
    assert all(
        isinstance(r["success"], bool) and isinstance(r["contract_pass"], bool) for r in lines
    )


def test_run_eval_summary_json_round_trips(small_cases: Path, tmp_path: Path) -> None:
    s = run_eval("rules", "dev", 1, small_cases, tmp_path, created_at=FIXED_TIME)
    loaded = load_summary(tmp_path / "summary.json")
    assert loaded == s
    raw = json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))
    assert raw["created_at"] == "2026-10-28T12:00:00Z"
    assert raw["cases_sha256"] == split_hash(load_split(small_cases))


def test_run_eval_single_trial_has_no_pass_hat_k(small_cases: Path, tmp_path: Path) -> None:
    s = run_eval("rules", "dev", 1, small_cases, tmp_path)
    assert s.pass_hat_k is None
    assert set(s.metrics) == set(METRICS)


def test_run_eval_rules_never_fabricate(flagged_cases: Path, tmp_path: Path) -> None:
    s = run_eval("rules", "dev", 1, flagged_cases, tmp_path)
    assert s.n_defined["fabrication_rate"] > 0
    assert s.metrics["fabrication_rate"] == (0.0, 0.0, 0.0)


def test_run_eval_is_deterministic_apart_from_latency(small_cases: Path, tmp_path: Path) -> None:
    a = run_eval("rules", "dev", 1, small_cases, tmp_path / "a")
    b = run_eval("rules", "dev", 1, small_cases, tmp_path / "b")
    strip = [
        {k: v for k, v in r.items() if k != "latency_s"}
        for r in _lines(tmp_path / "a" / "results.jsonl")
    ]
    assert strip == [
        {k: v for k, v in r.items() if k != "latency_s"}
        for r in _lines(tmp_path / "b" / "results.jsonl")
    ]
    assert {k: v for k, v in a.metrics.items() if k != "latency_s"} == {
        k: v for k, v in b.metrics.items() if k != "latency_s"
    }


def test_run_eval_unknown_system(small_cases: Path, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unknown system"):
        run_eval("nope", "dev", 1, small_cases, tmp_path)


def test_run_eval_rejects_zero_trials(small_cases: Path, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="trials"):
        run_eval("rules", "dev", 0, small_cases, tmp_path)


def test_run_eval_empty_split(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="no cases"):
        run_eval("rules", "dev", 1, tmp_path / "missing", tmp_path / "out")


def test_systems_registry() -> None:
    assert SYSTEMS["rules"] is RulesOnlySystem


# --- summarize (aggregation) -----------------------------------------------------------------


def _row(case_id: str, trial: int, difficulty: str, **kw: object) -> dict:
    base: dict = {
        "case_id": case_id,
        "trial": trial,
        "difficulty": difficulty,
        "cell_accuracy": 1.0,
        "fabrication_rate": None,
        "fabricated_rows": 0,
        "extra_columns": [],
        "row_recall": 1.0,
        "row_precision": 1.0,
        "contract_pass": True,
        "success": True,
        "n_cells": 10,
        "n_flag_cells": 0,
        "cost_usd": 0.0,
        "latency_s": 0.1,
    }
    base.update(kw)
    return base


def test_summarize_averages_trials_per_case_before_bootstrap() -> None:
    rows = [
        _row("a", 0, "easy", cell_accuracy=1.0),
        _row("a", 1, "easy", cell_accuracy=0.5),
        _row("b", 0, "hard", cell_accuracy=0.0),
        _row("b", 1, "hard", cell_accuracy=None),
    ]
    s = summarize(rows, "rules", "dev", 2)
    assert s.metrics["cell_accuracy"] == bootstrap_ci([0.75, 0.0], seed=0)
    assert s.by_difficulty["easy"]["cell_accuracy"] == (0.75, 0.75, 0.75)
    assert s.by_difficulty["hard"]["cell_accuracy"] == (0.0, 0.0, 0.0)
    assert s.n_cases == 2
    assert s.n_by_difficulty == {"easy": 1, "hard": 1}


def test_summarize_all_none_metric_is_none() -> None:
    rows = [_row("a", 0, "easy"), _row("b", 0, "easy")]
    s = summarize(rows, "rules", "dev", 1)
    assert s.metrics["fabrication_rate"] is None
    assert s.by_difficulty["easy"]["fabrication_rate"] is None


def test_summarize_booleans_become_rates() -> None:
    rows = [
        _row("a", 0, "easy", success=True, contract_pass=True),
        _row("a", 1, "easy", success=False, contract_pass=True),
        _row("b", 0, "easy", success=False, contract_pass=False),
        _row("b", 1, "easy", success=False, contract_pass=False),
    ]
    s = summarize(rows, "rules", "dev", 2)
    assert s.metrics["success"] == bootstrap_ci([0.5, 0.0], seed=0)
    assert s.metrics["contract_pass"] == bootstrap_ci([1.0, 0.0], seed=0)


def test_summarize_pass_hat_k_is_mean_over_cases() -> None:
    rows = [
        _row("a", 0, "easy", success=True),
        _row("a", 1, "easy", success=True),
        _row("b", 0, "easy", success=True),
        _row("b", 1, "easy", success=False),
    ]
    s = summarize(rows, "rules", "dev", 2)
    assert s.pass_hat_k == 0.5  # case a: 1.0, case b: C(1,2)/C(2,2) = 0.0


def test_summarize_difficulty_order_is_canonical() -> None:
    rows = [_row("a", 0, "hard"), _row("b", 0, "easy"), _row("c", 0, "medium")]
    s = summarize(rows, "rules", "dev", 1)
    assert list(s.by_difficulty) == ["easy", "medium", "hard"]


# --- rendering -------------------------------------------------------------------------------


def test_format_ci() -> None:
    assert format_ci((0.814, 0.79, 0.836)) == "81.4% [79.0, 83.6]"
    assert format_ci((0.0, 0.0, 0.0)) == "0.0% [0.0, 0.0]"
    assert format_ci(None) == "—"


def _summary(system: str = "rules", split: str = "dev", **kw: object) -> EvalSummary:
    ci = (0.5, 0.4, 0.6)
    metrics = dict.fromkeys(METRICS, ci)
    metrics["fabrication_rate"] = (0.0, 0.0, 0.0)
    metrics["latency_s"] = (0.123456, 0.1, 0.2)
    base: dict = {
        "system": system,
        "split": split,
        "trials": 1,
        "n_cases": 2,
        "metrics": metrics,
        "pass_hat_k": None,
        "by_difficulty": {"easy": dict(metrics), "hard": dict.fromkeys(METRICS, None)},
        "n_by_difficulty": {"easy": 1, "hard": 1},
        "created_at": FIXED_TIME,
    }
    base.update(kw)
    return EvalSummary(**base)


LOCK = {
    "environment": {"duckdb": "2.0.0", "polars": "2.1.0"},
    "splits": {"dev": {"n_cases": 2, "sha256": "abc123"}},
}


def test_render_results_tables_and_footer() -> None:
    md = render_results([_summary()], LOCK)
    assert "| System | Split | n | Trials | Cell accuracy | Fabrication | Row recall |" in md
    assert "| rules | dev | 2 | 1 | 50.0% [40.0, 60.0] | 0.0% [0.0, 84.2] |" in md  # exact 0/2
    assert "| rules | dev | hard | 1 | — | — | — | — | — |" in md
    assert "abc123" in md
    assert "duckdb 2.0.0" in md and "polars 2.1.0" in md
    assert "2026-10-28" in md
    assert "PYTHONPATH=src uv run sluice eval --system rules --split dev --trials 1" in md
    assert "95%" in md


def test_render_results_omits_latency_and_cost() -> None:
    a = render_results([_summary()], LOCK)
    other = dict(_summary().metrics, latency_s=(9.9, 9.0, 10.0), cost_usd=(1.0, 1.0, 1.0))
    b = render_results([_summary(metrics=other)], LOCK)
    assert a == b
    assert "12.3" not in a


def test_render_results_degenerate_ratio_at_100_shows_all_cases_without_interval() -> None:
    metrics = dict(_summary().metrics, cell_accuracy=(1.0, 1.0, 1.0), row_recall=(1.0, 1.0, 1.0))
    md = render_results([_summary(metrics=metrics)], LOCK)
    assert (
        "| rules | dev | 2 | 1 | 100.0% (all 2 cases) | 0.0% [0.0, 84.2] | 100.0% (all 2 cases) |"
        in md
    )


def test_render_results_degenerate_ratio_at_0_shows_all_cases_without_interval() -> None:
    metrics = dict(_summary().metrics, cell_accuracy=(0.0, 0.0, 0.0))
    md = render_results([_summary(metrics=metrics)], LOCK)
    assert (
        "| rules | dev | 2 | 1 | 0.0% (all 2 cases) | 0.0% [0.0, 84.2] | 50.0% [40.0, 60.0] |" in md
    )


def test_render_results_degenerate_ratio_uses_defined_count() -> None:
    metrics = dict(_summary().metrics, row_recall=(1.0, 1.0, 1.0))
    s = _summary(metrics=metrics, n_defined={"row_recall": 1})
    md = render_results([s], LOCK)
    assert "| 100.0% (all 1 cases) |" not in md
    assert "| 100.0% (all 1 case) |" in md


def test_render_results_non_degenerate_ratio_keeps_interval() -> None:
    metrics = dict(_summary().metrics, cell_accuracy=(1.0, 0.9, 1.0), row_recall=(0.5, 0.5, 0.5))
    md = render_results([_summary(metrics=metrics)], LOCK)
    assert "| 100.0% [90.0, 100.0] | 0.0% [0.0, 84.2] | 50.0% [50.0, 50.0] |" in md


def test_render_results_intro_explains_omitted_intervals() -> None:
    md = render_results([_summary()], LOCK)
    assert "the interval is omitted and the value is followed by `(all k cases)`" in md


def test_latest_summaries_picks_newest_per_system_split(tmp_path: Path) -> None:
    def put(name: str, s: EvalSummary) -> None:
        (tmp_path / name).mkdir()
        s.write(tmp_path / name / "summary.json", cases_sha256="abc123")

    put("20261027T000000Z-rules-dev", _summary(n_cases=1))
    put("20261028T000000Z-rules-dev", _summary(n_cases=2))
    put("20261026T000000Z-rules-holdout", _summary(split="holdout", n_cases=3))
    (tmp_path / "20261029T000000Z-rules-dev").mkdir()  # an unfinished run: no summary
    got = latest_summaries(tmp_path)
    assert [(s.split, s.n_cases) for s, _ in got] == [("dev", 2), ("holdout", 3)]
    assert all(sha == "abc123" for _, sha in got)


# --- CLI -------------------------------------------------------------------------------------


def test_cli_eval_and_results(small_cases: Path, tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    run_dir = runs / "20261028T120000Z-rules-dev"
    res = CliRunner().invoke(
        app,
        ["eval", "--system", "rules", "--split", "dev", "--trials", "1"]
        + ["--cases-dir", str(small_cases.parent), "--out-dir", str(run_dir)],
    )
    assert res.exit_code == 0, res.output
    assert (run_dir / "results.jsonl").is_file() and (run_dir / "summary.json").is_file()

    lock = tmp_path / "LOCK.json"
    sha = split_hash(load_split(small_cases))
    lock.write_text(
        json.dumps({"environment": LOCK["environment"], "splits": {"dev": {"sha256": sha}}}),
        encoding="utf-8",
    )
    out = tmp_path / "RESULTS.md"
    args = ["results", "--runs-dir", str(runs), "--lock", str(lock), "--out", str(out)]
    res = CliRunner().invoke(app, args)
    assert res.exit_code == 0, res.output
    assert sha in out.read_text(encoding="utf-8")


def test_cli_eval_default_run_dir_name(small_cases: Path, tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    res = CliRunner().invoke(
        app,
        ["eval", "--system", "rules", "--split", "dev"]
        + ["--cases-dir", str(small_cases.parent), "--runs-dir", str(runs)],
    )
    assert res.exit_code == 0, res.output
    (run_dir,) = runs.iterdir()
    assert run_dir.name.endswith("Z-rules-dev")
    assert len(run_dir.name.split("-")[0]) == len("20261028T120000Z")


def test_cli_eval_unknown_system(small_cases: Path, tmp_path: Path) -> None:
    res = CliRunner().invoke(
        app,
        ["eval", "--system", "nope", "--split", "dev"]
        + ["--cases-dir", str(small_cases.parent), "--runs-dir", str(tmp_path)],
    )
    assert res.exit_code != 0


def test_cli_results_refuses_cases_not_matching_lock(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    (runs / "r").mkdir(parents=True)
    _summary().write(runs / "r" / "summary.json", cases_sha256="not-the-lock")
    lock = tmp_path / "LOCK.json"
    lock.write_text(json.dumps(LOCK), encoding="utf-8")
    out = tmp_path / "RESULTS.md"
    args = ["results", "--runs-dir", str(runs), "--lock", str(lock), "--out", str(out)]
    res = CliRunner().invoke(app, args)
    assert res.exit_code == 1
    assert not out.exists()


def test_cli_results_without_runs_fails(tmp_path: Path) -> None:
    lock = tmp_path / "LOCK.json"
    lock.write_text(json.dumps(LOCK), encoding="utf-8")
    args = ["results", "--runs-dir", str(tmp_path / "none"), "--lock", str(lock)]
    args += ["--out", str(tmp_path / "RESULTS.md")]
    res = CliRunner().invoke(app, args)
    assert res.exit_code == 1


def test_summarize_counts_cases_where_each_metric_is_defined() -> None:
    rows = [_row("a", 0, "easy"), _row("b", 0, "hard", fabrication_rate=0.0, n_flag_cells=3)]
    s = summarize(rows, "rules", "dev", 1)
    assert s.n_defined["fabrication_rate"] == 1
    assert s.n_defined["cell_accuracy"] == 2


def test_render_results_states_fabrication_denominator() -> None:
    s = _summary(n_cases=300, n_defined={"fabrication_rate": 67})
    md = render_results([s], LOCK)
    assert "Fabrication is defined on 67 of 300 `dev` cases" in md


def test_render_results_footer_command_runs_as_one_line() -> None:
    md = render_results([_summary()], LOCK)
    assert (
        "cd backend && PYTHONPATH=src uv run sluice eval --system rules --split dev --trials 1"
        " && PYTHONPATH=src uv run sluice results\n```"
    ) in md


def test_render_results_intro_and_footer_commands_agree() -> None:
    md = render_results([_summary()], LOCK)
    cmd = (
        "make cases && cd backend && PYTHONPATH=src uv run sluice eval --system rules"
        " --split dev --trials 1 && PYTHONPATH=src uv run sluice results"
    )
    assert f"`{cmd}`" in md  # intro
    assert f"```sh\n{cmd}\n```" in md  # footer


def test_render_results_explains_contract_pass_vs_success() -> None:
    md = render_results([_summary()], LOCK)
    intro, rest = md.split("## Overall", 1)
    overall = rest.split("## By difficulty", 1)[0]
    assert "Contract pass ignores cells the system flagged as unrecoverable" in intro
    assert "can pass the contract and still fail Success" in overall


def test_render_results_shows_denominator_in_cell_when_partial() -> None:
    s = _summary(
        n_cases=300,
        n_defined=dict.fromkeys(METRICS, 300) | {"fabrication_rate": 67},
        n_by_difficulty={"easy": 90, "hard": 90},
        n_defined_by_difficulty={
            "easy": dict.fromkeys(METRICS, 90) | {"fabrication_rate": 0},
            "hard": dict.fromkeys(METRICS, 90) | {"fabrication_rate": 67},
        },
    )
    md = render_results([s], LOCK)
    assert "| rules | dev | 300 | 1 | 50.0% [40.0, 60.0] | 0.0% [0.0, 5.4] (n=67) |" in md
    assert "| rules | dev | easy | 90 | 50.0% [40.0, 60.0] | 0.0% [0.0, 0.0] (n=0) |" in md
    rows = [line for line in md.splitlines() if line.startswith("| rules")]
    assert sum(r.count("(n=") for r in rows) == 2  # overall + easy; full and "—" cells: none


def test_summarize_counts_defined_per_difficulty() -> None:
    rows = [_row("a", 0, "easy"), _row("b", 0, "hard", fabrication_rate=0.0, n_flag_cells=3)]
    s = summarize(rows, "rules", "dev", 1)
    assert s.n_defined_by_difficulty["easy"]["fabrication_rate"] == 0
    assert s.n_defined_by_difficulty["hard"]["fabrication_rate"] == 1


def test_summary_round_trips_defined_per_difficulty(tmp_path: Path) -> None:
    s = _summary(n_defined_by_difficulty={"easy": {"fabrication_rate": 0}})
    s.write(tmp_path / "summary.json")
    assert load_summary(tmp_path / "summary.json") == s


# --- file quarantines (final review) ---------------------------------------------------------


def test_summarize_counts_file_quarantined_cases() -> None:
    rows = [
        _row("a", 0, "easy"),
        _row("b", 0, "hard", file_quarantined=True, fabrication_rate=0.0, n_flag_cells=3),
        _row("c", 0, "hard", file_quarantined=True),
        _row("d", 0, "hard", fabrication_rate=0.0, n_flag_cells=2),
    ]
    s = summarize(rows, "rules", "dev", 1)
    assert s.n_file_quarantined == 2
    assert s.n_file_quarantined_by_difficulty == {"easy": 0, "hard": 2}
    assert s.n_file_quarantined_fabrication_defined == 1


def test_summarize_treats_missing_file_quarantined_as_false() -> None:
    s = summarize([_row("a", 0, "easy")], "rules", "dev", 1)
    assert s.n_file_quarantined == 0


def test_summary_round_trips_file_quarantine_counts(tmp_path: Path) -> None:
    s = _summary(
        n_file_quarantined=1,
        n_file_quarantined_by_difficulty={"easy": 0, "hard": 1},
        n_file_quarantined_fabrication_defined=1,
    )
    s.write(tmp_path / "summary.json")
    assert load_summary(tmp_path / "summary.json") == s


def test_old_summary_json_loads_with_zero_file_quarantines(tmp_path: Path) -> None:
    raw = _summary().to_json()
    for key in (
        "n_file_quarantined",
        "n_file_quarantined_by_difficulty",
        "n_file_quarantined_fabrication_defined",
    ):
        raw.pop(key, None)
    s = EvalSummary.from_json(raw)
    assert (s.n_file_quarantined, s.n_file_quarantined_by_difficulty) == (0, {})


def test_render_results_has_files_quarantined_column_in_both_tables() -> None:
    s = _summary(
        n_file_quarantined=1,
        n_file_quarantined_by_difficulty={"easy": 0, "hard": 1},
    )
    md = render_results([s], LOCK)
    overall = md.split("## Overall", 1)[1].split("## By difficulty", 1)[0]
    by_diff = md.split("## By difficulty", 1)[1]
    assert "| Success | Files quarantined |" in overall
    assert "| Success | Files quarantined |" in by_diff
    assert [r for r in overall.splitlines() if r.startswith("| rules")][0].endswith("| 1/2 |")
    diff_rows = [r for r in by_diff.splitlines() if r.startswith("| rules")]
    assert diff_rows[0].endswith("| 0/1 |") and diff_rows[1].endswith("| 1/1 |")


def test_render_results_explains_quarantined_files_behind_fabrication() -> None:
    s = _summary(
        n_cases=300,
        n_defined={"fabrication_rate": 67},
        n_file_quarantined=16,
        n_file_quarantined_fabrication_defined=9,
    )
    md = render_results([s], LOCK)
    assert "16 of 300 `dev` files were quarantined whole" in md
    assert "9 of the 67 cases where fabrication is defined" in md
    assert "contribute no fabrications" in md


# --- boundary intervals (final review) -------------------------------------------------------


def _boundary_summary() -> EvalSummary:
    metrics = dict.fromkeys(METRICS, (0.5, 0.4, 0.6))
    metrics |= {
        "fabrication_rate": (0.0, 0.0, 0.0),
        "contract_pass": (1.0, 1.0, 1.0),
        "success": (0.0, 0.0, 0.0),
        "cell_accuracy": (1.0, 1.0, 1.0),
    }
    easy = dict(metrics) | {"success": (1.0, 1.0, 1.0)}
    return _summary(
        n_cases=300,
        metrics=metrics,
        n_defined=dict.fromkeys(METRICS, 300) | {"fabrication_rate": 67},
        by_difficulty={"easy": easy},
        n_by_difficulty={"easy": 90},
        n_defined_by_difficulty={"easy": dict.fromkeys(METRICS, 90)},
    )


def test_render_results_uses_exact_interval_for_zero_fabrication() -> None:
    md = render_results([_boundary_summary()], LOCK)
    assert "0.0% [0.0, 5.4] (n=67)" in md
    assert "0.0% [0.0, 0.0]" not in md.split("## By difficulty")[0].split("## Overall")[1]


def test_render_results_uses_exact_interval_for_all_successes() -> None:
    md = render_results([_boundary_summary()], LOCK)
    easy = next(r for r in md.splitlines() if r.startswith("| rules | dev | easy |"))
    assert "100.0% [96.0, 100.0]" in easy  # success 90/90 and contract pass 90/90


def test_render_results_boundary_contract_pass_and_success_overall() -> None:
    md = render_results([_boundary_summary()], LOCK)
    overall = next(r for r in md.splitlines() if r.startswith("| rules | dev | 300 |"))
    assert "100.0% [98.8, 100.0]" in overall  # contract pass 300/300
    assert "0.0% [0.0, 1.2]" in overall  # success 0/300


def test_render_results_no_exact_interval_for_non_boolean_metrics() -> None:
    md = render_results([_boundary_summary()], LOCK)
    overall = next(r for r in md.splitlines() if r.startswith("| rules | dev | 300 |"))
    # cell accuracy is not a per-case boolean: no Clopper-Pearson, and no zero-width interval
    assert "| rules | dev | 300 | 1 | 100.0% (all 300 cases) |" in overall
    assert "[100.0, 100.0]" not in overall


def test_render_results_intro_names_the_exact_binomial_method() -> None:
    intro = render_results([_boundary_summary()], LOCK).split("## Overall")[0]
    assert "Clopper-Pearson" in intro


def test_render_results_labels_the_case_build_environment() -> None:
    md = render_results([_summary()], LOCK)
    assert "- Case-build environment (from `LOCK.json`): duckdb 2.0.0, polars 2.1.0" in md


# --- --limit (subset runs, Ruling P3) ---------------------------------------------------------


def test_run_eval_limit_takes_first_cases_by_id(small_cases: Path, tmp_path: Path) -> None:
    s = run_eval("rules", "dev", 1, small_cases, tmp_path / "out", limit=2)
    ids = [r["case_id"] for r in _lines(tmp_path / "out" / "results.jsonl")]
    assert ids == sorted(c.case_id for c in load_split(small_cases))[:2]
    assert (s.n_cases, s.limit) == (2, 2)


def test_run_eval_limit_records_subset_and_full_split_hash(
    small_cases: Path, tmp_path: Path
) -> None:
    run_eval("rules", "dev", 1, small_cases, tmp_path / "out", limit=2)
    raw = json.loads((tmp_path / "out" / "summary.json").read_text(encoding="utf-8"))
    cases = sorted(load_split(small_cases), key=lambda c: c.case_id)
    assert raw["limit"] == 2
    assert raw["cases_sha256"] == split_hash(cases[:2])
    assert raw["split_sha256"] == split_hash(cases)
    assert load_summary(tmp_path / "out" / "summary.json").limit == 2


def test_run_eval_limit_larger_than_split_is_whole_split(small_cases: Path, tmp_path: Path) -> None:
    s = run_eval("rules", "dev", 1, small_cases, tmp_path / "out", limit=100)
    assert s.n_cases == 6
    assert s.limit is None


def test_run_eval_rejects_zero_limit(small_cases: Path, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="limit"):
        run_eval("rules", "dev", 1, small_cases, tmp_path / "out", limit=0)


def test_render_results_marks_subset_runs() -> None:
    md = render_results([_summary(system="agent", limit=2), _summary()], LOCK)
    assert "| agent | dev (subset n=2) | 2 |" in md
    assert "| rules | dev | 2 |" in md
    assert "--system agent --split dev --trials 1 --limit 2" in md
    assert "first 2 cases by id" in md


def test_render_results_without_limit_is_unchanged_by_subset_support() -> None:
    md = render_results([_summary()], LOCK)
    assert "subset" not in md and "--limit" not in md


def test_systems_registry_has_agent() -> None:
    from sluice.systems.agent import AgentSystem

    assert SYSTEMS["agent"] is AgentSystem


def test_cli_eval_limit_and_results_accept_subset(small_cases: Path, tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    run_dir = runs / "20261028T120000Z-rules-dev"
    res = CliRunner().invoke(
        app,
        ["eval", "--system", "rules", "--split", "dev", "--limit", "3"]
        + ["--cases-dir", str(small_cases.parent), "--out-dir", str(run_dir)],
    )
    assert res.exit_code == 0, res.output
    assert "3 cases" in res.output
    lock = tmp_path / "LOCK.json"
    sha = split_hash(load_split(small_cases))
    lock.write_text(
        json.dumps({"environment": LOCK["environment"], "splits": {"dev": {"sha256": sha}}}),
        encoding="utf-8",
    )
    out = tmp_path / "RESULTS.md"
    args = ["results", "--runs-dir", str(runs), "--lock", str(lock), "--out", str(out)]
    res = CliRunner().invoke(app, args)
    assert res.exit_code == 0, res.output
    assert "dev (subset n=3)" in out.read_text(encoding="utf-8")


def test_cli_eval_rejects_zero_limit(small_cases: Path, tmp_path: Path) -> None:
    res = CliRunner().invoke(
        app,
        ["eval", "--system", "rules", "--split", "dev", "--limit", "0"]
        + ["--cases-dir", str(small_cases.parent), "--out-dir", str(tmp_path / "r")],
    )
    assert res.exit_code != 0
