"""``GET /evals/summary``: the latest agent eval run and its peers, contract-shaped."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sluice.api.app import create_app
from sluice.api.routes_evals import DEFAULT_EVALS_DIR, p95_latency_ms
from sluice.api.settings import ApiSettings

REPO_EVALS = Path(__file__).resolve().parents[3] / "evals"
PUBLISHED = REPO_EVALS / "published"
AGENT_RUN = "20261029T031347Z-agent-dev"
SYSTEM_KEYS = {
    "system",
    "cellAccuracy",
    "fabricationRate",
    "contractPassRate",
    "passHat4",
    "p95LatencyMs",
    "costPer1kRowsUsd",
}


def _app(evals_dir: Path) -> FastAPI:
    app = create_app(settings=ApiSettings(), worker=False)
    app.state.evals_dir = evals_dir
    return app


@pytest.fixture
def evals_dir(tmp_path: Path) -> Path:
    root = tmp_path / "evals"
    (root / "runs").mkdir(parents=True)
    (root / "published").mkdir()
    return root


def _get(evals_dir: Path) -> Any:
    with TestClient(_app(evals_dir), raise_server_exceptions=False) as client:
        return client.get("/evals/summary")


def _summary(
    system: str,
    *,
    split: str = "dev",
    created: str = "2026-10-29T03:00:00Z",
    trials: int = 1,
    limit: int | None = None,
    pass_hat_k: float | None = None,
    n_cases: int = 3,
) -> dict[str, Any]:
    metrics = {
        "cell_accuracy": [0.8, 0.7, 0.9],
        "fabrication_rate": None,
        "row_recall": [1.0, 1.0, 1.0],
        "row_precision": [1.0, 1.0, 1.0],
        "contract_pass": [0.5, 0.2, 0.8],
        "success": [0.5, 0.2, 0.8],
        "cost_usd": [0.0, 0.0, 0.0],
        "latency_s": [1.0, 0.5, 1.5],
    }
    out: dict[str, Any] = {
        "system": system,
        "split": split,
        "trials": trials,
        "n_cases": n_cases,
        "metrics": metrics,
        "pass_hat_k": pass_hat_k,
        "by_difficulty": {},
        "created_at": created,
    }
    if limit is not None:
        out["limit"] = limit
    return out


def _write(
    root: Path, name: str, summary: dict[str, Any], results: list[dict[str, Any]] | None = None
) -> Path:
    run = root / name
    run.mkdir(parents=True)
    (run / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
    if results is not None:
        lines = "".join(json.dumps(r) + "\n" for r in results)
        (run / "results.jsonl").write_text(lines, encoding="utf-8")
    return run


def test_default_evals_dir_is_repo_evals() -> None:
    assert DEFAULT_EVALS_DIR == REPO_EVALS


def test_evals_summary_from_committed_fixtures(evals_dir: Path) -> None:
    shutil.copytree(PUBLISHED, evals_dir / "published", dirs_exist_ok=True)
    res = _get(evals_dir)
    assert res.status_code == 200
    body = res.json()
    assert set(body) == {"split", "n", "generatedAt", "systems", "pareto"}
    assert body["split"] == "dev"
    assert body["n"] == 30
    assert body["generatedAt"] == "2026-10-29T03:13:47.000Z"
    assert body["pareto"] == []
    assert [s["system"] for s in body["systems"]] == ["sluice", "rules"]
    sluice = body["systems"][0]
    assert set(sluice) == SYSTEM_KEYS
    assert sluice["cellAccuracy"]["mean"] == pytest.approx(0.9667, abs=1e-4)
    assert sluice["cellAccuracy"]["lo"] == pytest.approx(0.9)
    assert sluice["fabricationRate"] == {"mean": 0.0, "lo": 0.0, "hi": 0.0}
    assert sluice["contractPassRate"]["mean"] == pytest.approx(0.9667, abs=1e-4)
    assert sluice["passHat4"] is None
    assert sluice["costPer1kRowsUsd"] is None


def test_evals_summary_p95_latency_from_results(evals_dir: Path) -> None:
    shutil.copytree(PUBLISHED, evals_dir / "published", dirs_exist_ok=True)
    sluice = _get(evals_dir).json()["systems"][0]
    lines = (PUBLISHED / AGENT_RUN / "results.jsonl").read_text().splitlines()
    latencies = [json.loads(line)["latency_s"] for line in lines]
    p95 = sluice["p95LatencyMs"]
    assert p95["mean"] == pytest.approx(float(np.percentile(latencies, 95)) * 1000)
    assert p95["lo"] <= p95["mean"] <= p95["hi"]


def test_evals_summary_default_dir_serves_repo_fixtures() -> None:
    with TestClient(create_app(settings=ApiSettings(), worker=False)) as client:
        res = client.get("/evals/summary")
    assert res.status_code == 200
    assert res.json()["systems"][0]["system"] == "sluice"


def test_evals_summary_without_agent_is_404_no_evals(evals_dir: Path) -> None:
    _write(evals_dir / "runs", "20261029T000000Z-rules-dev", _summary("rules"))
    res = _get(evals_dir)
    assert res.status_code == 404
    assert res.json() == {"detail": res.json()["detail"], "code": "no_evals"}


def test_evals_summary_empty_dirs_is_404(tmp_path: Path) -> None:
    res = _get(tmp_path / "missing")
    assert res.status_code == 404
    assert res.json()["code"] == "no_evals"


def test_evals_summary_newest_agent_picks_split_and_peers(evals_dir: Path) -> None:
    runs = evals_dir / "runs"
    _write(runs, "a1", _summary("agent", split="dev", created="2026-10-01T00:00:00Z"))
    newer = _summary("agent", split="holdout", created="2026-10-02T00:00:00Z", n_cases=7)
    _write(runs, "a2", newer)
    _write(runs, "r1", _summary("rules", split="holdout"))
    _write(runs, "r2", _summary("rules", split="dev"))
    _write(runs, "r3", _summary("rules", split="holdout", limit=5))
    body = _get(evals_dir).json()
    assert body["split"] == "holdout"
    assert body["n"] == 7
    assert [s["system"] for s in body["systems"]] == ["sluice", "rules"]


def test_evals_summary_missing_results_gives_null_latency(evals_dir: Path) -> None:
    _write(evals_dir / "runs", "a", _summary("agent"))
    sluice = _get(evals_dir).json()["systems"][0]
    assert sluice["p95LatencyMs"] is None
    assert sluice["fabricationRate"] is None
    assert sluice["cellAccuracy"] == {"mean": 0.8, "lo": 0.7, "hi": 0.9}


def test_evals_summary_pass_hat4_when_four_trials(evals_dir: Path) -> None:
    results = [
        {"case_id": f"c{c}", "trial": t, "success": not (c == 0 and t == 3), "latency_s": 1.0}
        for c in range(3)
        for t in range(4)
    ]
    _write(evals_dir / "runs", "a", _summary("agent", trials=4, pass_hat_k=2 / 3), results=results)
    sluice = _get(evals_dir).json()["systems"][0]
    assert sluice["passHat4"]["mean"] == pytest.approx(2 / 3)
    assert 0.0 <= sluice["passHat4"]["lo"] <= sluice["passHat4"]["hi"] <= 1.0
    assert sluice["p95LatencyMs"] == {"mean": 1000.0, "lo": 1000.0, "hi": 1000.0}


def test_evals_summary_pass_hat4_null_when_not_four_trials(evals_dir: Path) -> None:
    _write(evals_dir / "runs", "a", _summary("agent", trials=2, pass_hat_k=0.5))
    assert _get(evals_dir).json()["systems"][0]["passHat4"] is None


def test_p95_latency_averages_trials_per_case() -> None:
    rows = [
        {"case_id": "a", "latency_s": 1.0},
        {"case_id": "a", "latency_s": 3.0},
        {"case_id": "b", "latency_s": 4.0},
        {"case_id": "c", "latency_s": None},
    ]
    ci = p95_latency_ms(rows)
    assert ci is not None
    assert ci.mean == pytest.approx(float(np.percentile([2.0, 4.0], 95)) * 1000)


def test_p95_latency_none_without_values() -> None:
    assert p95_latency_ms([{"case_id": "a", "latency_s": None}]) is None
    assert p95_latency_ms([]) is None


def test_evals_summary_reflects_new_runs(evals_dir: Path) -> None:
    runs = evals_dir / "runs"
    _write(runs, "a1", _summary("agent", created="2026-10-01T00:00:00Z"))
    app = _app(evals_dir)
    with TestClient(app) as client:
        first = client.get("/evals/summary").json()
        _write(runs, "a2", _summary("agent", split="holdout", created="2026-10-05T00:00:00Z"))
        second = client.get("/evals/summary").json()
    assert first["split"] == "dev"
    assert second["split"] == "holdout"
