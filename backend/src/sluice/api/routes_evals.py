"""``GET /evals/summary``: the latest scored agent eval run and its peers, contract-shaped.

Built from :func:`sluice.evals.runner.latest_summaries` over ``<evals_dir>/runs`` (fresh,
git-ignored) and ``<evals_dir>/published`` (committed). The newest ``agent`` summary fixes
``split``, ``n``, ``generatedAt`` and the subset (``limit``); ``systems`` holds it (named
``sluice``) and every other system's summary for the same ``(split, limit)``, by name.

Metrics come from the summary CIs. ``passHat4`` only exists for 4-trial runs: the bootstrap of
the per-case pass^4 when ``results.jsonl`` is present
(``mean`` is always the summary's ``pass_hat_k``), else the summary point (zero width).
``p95LatencyMs`` is the 95th percentile of per-case latency (trials averaged) with a
percentile-bootstrap CI, ``None`` without ``results.jsonl``. ``costPer1kRowsUsd`` is ``None``
(rows per case are not recorded yet) and ``pareto`` is empty until the router exists.

Cost: a few small JSON files plus one ``results.jsonl`` (streamed line by line) per system, and
two bootstraps of ``n_cases`` values: a few milliseconds, run in a thread.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterable, Iterator, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, get_args

from fastapi import APIRouter, Request

from sluice.api.errors import ApiError
from sluice.api.schemas import CI, EvalSummary, SystemMetrics
from sluice.api.store import iso_utc
from sluice.evals import runner
from sluice.evals.stats import bootstrap_ci, bootstrap_quantile_ci, pass_hat_k

router = APIRouter()

DEFAULT_EVALS_DIR: Final = Path(__file__).resolve().parents[4] / "evals"
"""``<repo>/evals`` (this file is ``<repo>/backend/src/sluice/api/routes_evals.py``)."""
AGENT_SYSTEM: Final = "agent"
AGENT_DISPLAY_NAME: Final = "sluice"
PASS_HAT_TRIALS: Final = 4
LATENCY_QUANTILE: Final = 0.95
SPLITS: Final = frozenset(get_args(EvalSummary.model_fields["split"].annotation))
NO_EVALS_DETAIL: Final = "No eval results have been published yet."
_EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)

Summary = runner.EvalSummary


def _ci(raw: tuple[float, float, float] | None) -> CI | None:
    return None if raw is None else CI(mean=raw[0], lo=raw[1], hi=raw[2])


def _aware(moment: datetime | None) -> datetime:
    if moment is None:
        return _EPOCH
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def _run_dirs(roots: Iterable[Path]) -> Iterator[Path]:
    for root in roots:
        if root.is_dir():
            yield from sorted(p for p in root.iterdir() if (p / runner.SUMMARY_FILE).is_file())


def _find_run(summary: Summary, roots: list[Path]) -> Path | None:
    """The run directory holding ``summary`` (``latest_summaries`` does not return paths)."""
    for run in _run_dirs(roots):
        raw = json.loads((run / runner.SUMMARY_FILE).read_text(encoding="utf-8"))
        if Summary.from_json(raw) == summary:
            return run
    return None


def _results(run: Path | None) -> list[dict[str, Any]] | None:
    """The per-(case, trial) rows of ``results.jsonl``, or ``None`` if there is none."""
    if run is None or not (run / runner.RESULTS_FILE).is_file():
        return None
    with (run / runner.RESULTS_FILE).open(encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def p95_latency_ms(rows: Iterable[Mapping[str, Any]]) -> CI | None:
    """95th percentile of per-case latency (mean over trials) in ms, with a bootstrap CI."""
    sums: dict[str, tuple[float, int]] = {}
    for row in rows:
        latency = row.get("latency_s")
        if latency is None:
            continue
        total, count = sums.get(row["case_id"], (0.0, 0))
        sums[row["case_id"]] = (total + float(latency), count + 1)
    per_case = [total / count for total, count in sums.values()]
    found = bootstrap_quantile_ci(per_case, LATENCY_QUANTILE, seed=runner.BOOTSTRAP_SEED)
    if found is None:
        return None
    mean, lo, hi = (value * 1000 for value in found)
    return CI(mean=mean, lo=lo, hi=hi)


def _pass_hat4(summary: Summary, rows: list[dict[str, Any]] | None) -> CI | None:
    """``mean`` is exactly the summary's ``pass_hat_k``; only ``lo``/``hi`` are bootstrapped.

    The interval comes from a bootstrap of the per-case pass^4 in ``results.jsonl`` (widened to
    contain the point if needed); zero width at the point without per-case rows.
    """
    if summary.trials != PASS_HAT_TRIALS or summary.pass_hat_k is None:
        return None
    point = float(summary.pass_hat_k)
    if rows:
        counts: dict[str, tuple[int, int]] = {}
        for row in rows:
            trials, ok = counts.get(row["case_id"], (0, 0))
            counts[row["case_id"]] = (trials + 1, ok + bool(row.get("success")))
        per_case = [
            pass_hat_k(n, c, PASS_HAT_TRIALS) for n, c in counts.values() if n >= PASS_HAT_TRIALS
        ]
        found = bootstrap_ci(per_case, seed=runner.BOOTSTRAP_SEED)
        if found is not None:
            return CI(mean=point, lo=min(found[1], point), hi=max(found[2], point))
    return CI(mean=point, lo=point, hi=point)


def _metrics(summary: Summary, roots: list[Path], name: str) -> SystemMetrics:
    rows = _results(_find_run(summary, roots))
    return SystemMetrics(
        system=name,
        cell_accuracy=_ci(summary.metrics.get("cell_accuracy")),
        fabrication_rate=_ci(summary.metrics.get("fabrication_rate")),
        contract_pass_rate=_ci(summary.metrics.get("contract_pass")),
        pass_hat4=_pass_hat4(summary, rows),
        p95_latency_ms=None if rows is None else p95_latency_ms(rows),
        cost_per1k_rows_usd=None,
    )


def build_eval_summary(evals_dir: Path) -> EvalSummary:
    """The contract :class:`EvalSummary` for ``evals_dir`` (blocking file I/O).

    Raises:
        ApiError: 404 ``no_evals`` when there is no agent summary for a contract split.
    """
    runs_dir, published_dir = evals_dir / "runs", evals_dir / "published"
    found = [summary for summary, _ in runner.latest_summaries(runs_dir, published_dir)]
    agents = [s for s in found if s.system == AGENT_SYSTEM and s.split in SPLITS]
    if not agents:
        raise ApiError(404, NO_EVALS_DETAIL, "no_evals")
    agent = max(agents, key=lambda s: _aware(s.created_at))
    peers = sorted(
        (
            s
            for s in found
            if s.system != AGENT_SYSTEM and (s.split, s.limit) == (agent.split, agent.limit)
        ),
        key=lambda s: s.system,
    )
    roots = [published_dir, runs_dir]
    systems = [_metrics(agent, roots, AGENT_DISPLAY_NAME)]
    systems += [_metrics(s, roots, s.system) for s in peers]
    return EvalSummary(
        split=agent.split,  # type: ignore[arg-type]  # filtered to SPLITS above
        n=agent.n_cases,
        generated_at=iso_utc(_aware(agent.created_at)),
        systems=systems,
        pareto=[],
    )


@router.get("/evals/summary", response_model=EvalSummary)
async def eval_summary(request: Request) -> EvalSummary:
    """The latest scored eval run; ``404`` ``no_evals`` before any agent run exists."""
    return await asyncio.to_thread(build_eval_summary, request.app.state.evals_dir)
