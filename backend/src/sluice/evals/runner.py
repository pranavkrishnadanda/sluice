"""Eval runner: run a system over a split, grade every case x trial, and summarise with CIs.

A run writes two files into its output directory:

* ``results.jsonl`` -- one line per (case, trial), ordered by ``(case_id, trial)``, trials
  numbered from 0: ``case_id, trial, difficulty``, every :class:`~sluice.evals.graders.Grade`
  field (including ``file_quarantined``), then ``cost_usd`` and ``latency_s``. Booleans stay
  JSON booleans.
* ``summary.json`` -- the :class:`EvalSummary` plus ``created_at`` (UTC) and ``cases_sha256``
  (the :func:`~sluice.drift.cases.split_hash` of the cases actually evaluated).

Aggregation: for each case, each metric is averaged over its trials (``None`` trials skipped;
booleans as 0/1); :func:`~sluice.evals.stats.bootstrap_ci` (``seed=0``) then runs over the
per-case means, dropping cases where the metric is undefined. ``pass_hat_k`` is the mean over
cases of ``pass_hat_k(trials, successes, trials)``, ``None`` for a single trial. A case counts
as file-quarantined if any of its trials quarantined the whole file (rows without the field,
from older runs, count as not quarantined).

:func:`render_results` turns the latest summary per (system, split) into ``evals/RESULTS.md``.
Latency and cost are not rendered: latency is wall-clock, so the file stays byte-identical
across runs of the same code on the same cases (except the run date in the footer).

Cost: ``O(cases * trials)`` system runs and grades; rows are streamed to disk and kept as
small dicts (a few hundred bytes each) for the summary.
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sluice.contracts.model import Contract, load_contract
from sluice.drift.cases import load_reference, load_split, split_hash
from sluice.evals.graders import grade
from sluice.evals.stats import bootstrap_ci, clopper_pearson, pass_hat_k
from sluice.evals.types import CaseInput, System
from sluice.systems.rules_only import RulesOnlySystem

SYSTEMS: dict[str, Callable[[], System]] = {"rules": RulesOnlySystem}

SYSTEM_DESCRIPTIONS: dict[str, str] = {
    "rules": (
        "the rules-only baseline (deterministic profiling and repair rules, no model calls; "
        "it flags what it cannot recover instead of guessing)"
    ),
}

METRICS: tuple[str, ...] = (
    "cell_accuracy",
    "fabrication_rate",
    "row_recall",
    "row_precision",
    "contract_pass",
    "success",
    "cost_usd",
    "latency_s",
)
DIFFICULTIES: tuple[str, ...] = ("easy", "medium", "hard")
RESULTS_FILE = "results.jsonl"
SUMMARY_FILE = "summary.json"
BOOTSTRAP_SEED = 0

# Columns of the published tables: (header, metric key).
_TABLE_METRICS: tuple[tuple[str, str], ...] = (
    ("Cell accuracy", "cell_accuracy"),
    ("Fabrication", "fabrication_rate"),
    ("Row recall", "row_recall"),
    ("Contract pass", "contract_pass"),
    ("Success", "success"),
)
_QUARANTINE_HEADER = "Files quarantined"
# Metrics that are 0/1 per case (fabrication only at its bounds): exact interval at 0% / 100%.
_EXACT_AT_BOUNDARY: frozenset[str] = frozenset({"contract_pass", "success", "fabrication_rate"})
_NONE = "—"
_CLI = "cd backend && PYTHONPATH=src uv run sluice"

CI = tuple[float, float, float]


def _now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def _iso(ts: datetime) -> str:
    return ts.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ci_out(ci: CI | None) -> list[float] | None:
    return None if ci is None else list(ci)


def _ci_in(raw: Sequence[float] | None) -> CI | None:
    return None if raw is None else (float(raw[0]), float(raw[1]), float(raw[2]))


@dataclass(frozen=True)
class EvalSummary:
    """The headline numbers of one run (every CI is ``(mean, lo, hi)`` over cases)."""

    system: str
    split: str
    trials: int
    n_cases: int
    metrics: dict[str, CI | None]
    pass_hat_k: float | None
    by_difficulty: dict[str, dict[str, CI | None]]
    n_by_difficulty: dict[str, int] = field(default_factory=dict)
    n_defined: dict[str, int] = field(default_factory=dict)
    n_defined_by_difficulty: dict[str, dict[str, int]] = field(default_factory=dict)
    created_at: datetime | None = None
    n_file_quarantined: int = 0
    n_file_quarantined_by_difficulty: dict[str, int] = field(default_factory=dict)
    n_file_quarantined_fabrication_defined: int = 0

    def to_json(self, cases_sha256: str | None = None) -> dict[str, Any]:
        """JSON-ready dict; CIs become ``[mean, lo, hi]`` lists."""
        return {
            "system": self.system,
            "split": self.split,
            "trials": self.trials,
            "n_cases": self.n_cases,
            "metrics": {k: _ci_out(v) for k, v in self.metrics.items()},
            "pass_hat_k": self.pass_hat_k,
            "by_difficulty": {
                d: {k: _ci_out(v) for k, v in m.items()} for d, m in self.by_difficulty.items()
            },
            "n_by_difficulty": dict(self.n_by_difficulty),
            "n_defined": dict(self.n_defined),
            "n_defined_by_difficulty": {
                d: dict(m) for d, m in self.n_defined_by_difficulty.items()
            },
            "created_at": None if self.created_at is None else _iso(self.created_at),
            "n_file_quarantined": self.n_file_quarantined,
            "n_file_quarantined_by_difficulty": dict(self.n_file_quarantined_by_difficulty),
            "n_file_quarantined_fabrication_defined": self.n_file_quarantined_fabrication_defined,
            "cases_sha256": cases_sha256,
        }

    @classmethod
    def from_json(cls, raw: Mapping[str, Any]) -> EvalSummary:
        """Inverse of :meth:`to_json` (``cases_sha256`` is ignored)."""
        created = raw.get("created_at")
        return cls(
            system=raw["system"],
            split=raw["split"],
            trials=int(raw["trials"]),
            n_cases=int(raw["n_cases"]),
            metrics={k: _ci_in(v) for k, v in raw["metrics"].items()},
            pass_hat_k=raw["pass_hat_k"],
            by_difficulty={
                d: {k: _ci_in(v) for k, v in m.items()} for d, m in raw["by_difficulty"].items()
            },
            n_by_difficulty={d: int(n) for d, n in raw.get("n_by_difficulty", {}).items()},
            n_defined={m: int(n) for m, n in raw.get("n_defined", {}).items()},
            n_defined_by_difficulty={
                d: {m: int(n) for m, n in counts.items()}
                for d, counts in raw.get("n_defined_by_difficulty", {}).items()
            },
            created_at=None if created is None else datetime.fromisoformat(created),
            n_file_quarantined=int(raw.get("n_file_quarantined", 0)),
            n_file_quarantined_by_difficulty={
                d: int(n) for d, n in raw.get("n_file_quarantined_by_difficulty", {}).items()
            },
            n_file_quarantined_fabrication_defined=int(
                raw.get("n_file_quarantined_fabrication_defined", 0)
            ),
        )

    def write(self, path: Path, cases_sha256: str | None = None) -> None:
        """Write ``summary.json`` to ``path``."""
        text = json.dumps(self.to_json(cases_sha256), indent=2) + "\n"
        path.write_text(text, encoding="utf-8")


def load_summary(path: Path) -> EvalSummary:
    """Read a ``summary.json``."""
    return EvalSummary.from_json(json.loads(path.read_text(encoding="utf-8")))


def _mean(values: Iterable[Any]) -> float | None:
    kept = [float(v) for v in values if v is not None]
    return math.fsum(kept) / len(kept) if kept else None


def _difficulty_order(found: Iterable[str]) -> list[str]:
    present = set(found)
    return [d for d in DIFFICULTIES if d in present] + sorted(present - set(DIFFICULTIES))


def _defined(per_case: Sequence[Mapping[str, float | None]]) -> dict[str, int]:
    return {m: sum(1 for c in per_case if c[m] is not None) for m in METRICS}


def _cis(per_case: Sequence[Mapping[str, float | None]]) -> dict[str, CI | None]:
    return {m: bootstrap_ci([c[m] for c in per_case], seed=BOOTSTRAP_SEED) for m in METRICS}


def summarize(
    rows: Iterable[Mapping[str, Any]],
    system: str,
    split: str,
    trials: int,
    *,
    created_at: datetime | None = None,
) -> EvalSummary:
    """Aggregate ``results.jsonl`` rows into an :class:`EvalSummary` (module docstring).

    Raises:
        ValueError: if ``rows`` is empty or a case does not have exactly ``trials`` rows.
    """
    by_case: dict[str, list[Mapping[str, Any]]] = {}
    for row in rows:
        by_case.setdefault(row["case_id"], []).append(row)
    if not by_case:
        raise ValueError("no rows to summarise")

    per_case: list[dict[str, float | None]] = []
    quarantined: list[bool] = []
    difficulty: list[str] = []
    pass_hats: list[float] = []
    for case_id, case_rows in by_case.items():
        if len(case_rows) != trials:
            raise ValueError(f"{case_id}: {len(case_rows)} rows, expected {trials} trials")
        per_case.append({m: _mean(r[m] for r in case_rows) for m in METRICS})
        difficulty.append(case_rows[0]["difficulty"])
        quarantined.append(any(r.get("file_quarantined", False) for r in case_rows))
        if trials > 1:
            successes = sum(1 for r in case_rows if r["success"])
            pass_hats.append(pass_hat_k(trials, successes, trials))

    by_difficulty: dict[str, dict[str, CI | None]] = {}
    n_by_difficulty: dict[str, int] = {}
    n_defined_by_difficulty: dict[str, dict[str, int]] = {}
    n_quarantined_by_difficulty: dict[str, int] = {}
    for d in _difficulty_order(difficulty):
        group = [c for c, cd in zip(per_case, difficulty, strict=True) if cd == d]
        by_difficulty[d] = _cis(group)
        n_by_difficulty[d] = len(group)
        n_defined_by_difficulty[d] = _defined(group)
        n_quarantined_by_difficulty[d] = sum(
            q for q, cd in zip(quarantined, difficulty, strict=True) if cd == d
        )

    return EvalSummary(
        system=system,
        split=split,
        trials=trials,
        n_cases=len(per_case),
        metrics=_cis(per_case),
        pass_hat_k=math.fsum(pass_hats) / len(pass_hats) if pass_hats else None,
        by_difficulty=by_difficulty,
        n_by_difficulty=n_by_difficulty,
        n_defined=_defined(per_case),
        n_defined_by_difficulty=n_defined_by_difficulty,
        created_at=created_at,
        n_file_quarantined=sum(quarantined),
        n_file_quarantined_by_difficulty=n_quarantined_by_difficulty,
        n_file_quarantined_fabrication_defined=sum(
            q
            for q, c in zip(quarantined, per_case, strict=True)
            if c["fabrication_rate"] is not None
        ),
    )


def run_eval(
    system: str,
    split: str,
    trials: int,
    cases_dir: Path,
    out_dir: Path,
    *,
    created_at: datetime | None = None,
) -> EvalSummary:
    """Run ``system`` ``trials`` times on every case in ``cases_dir`` and grade each run.

    Args:
        system: A key of :data:`SYSTEMS`.
        split: The split name recorded in the summary (``dev`` / ``holdout``).
        trials: Runs per case (``>= 1``).
        cases_dir: The split directory holding ``<split>-NNNN/`` cases.
        out_dir: Where ``results.jsonl`` and ``summary.json`` are written (created if needed).
        created_at: Run timestamp recorded in the summary (default: now, UTC).

    Raises:
        ValueError: for an unknown system, ``trials < 1`` or an empty ``cases_dir``.
    """
    factory = SYSTEMS.get(system)
    if factory is None:
        raise ValueError(f"unknown system {system!r}; known: {', '.join(sorted(SYSTEMS))}")
    if trials < 1:
        raise ValueError(f"trials must be >= 1, got {trials}")
    cases = sorted(load_split(cases_dir), key=lambda c: c.case_id)
    if not cases:
        raise ValueError(f"no cases in {cases_dir}")
    created = (created_at or _now()).astimezone(UTC)

    sut = factory()
    contracts: dict[tuple[str, str], Contract] = {}
    references: dict[str, dict[str, set[Any]]] = {}
    rows: list[dict[str, Any]] = []
    out_dir.mkdir(parents=True, exist_ok=True)
    with (out_dir / RESULTS_FILE).open("w", encoding="utf-8") as fh:
        for case in cases:
            version = case.manifest.get("contract_version", "v1")
            key = (case.table, version)
            if key not in contracts:
                contracts[key] = load_contract(case.table, version)
            if case.table not in references:
                references[case.table] = load_reference(case.table)
            reference = references[case.table]
            for trial in range(trials):
                out = sut.run(CaseInput(case.input_path, contracts[key], reference))
                g = grade(case, out, reference)
                row = {
                    "case_id": g.case_id,
                    "trial": trial,
                    "difficulty": g.difficulty,
                    **asdict(g),
                    "cost_usd": float(out.cost_usd),
                    "latency_s": float(out.latency_s),
                }
                fh.write(json.dumps(row) + "\n")
                rows.append(row)

    summary = summarize(rows, system, split, trials, created_at=created)
    summary.write(out_dir / SUMMARY_FILE, cases_sha256=split_hash(cases))
    return summary


def latest_summaries(runs_dir: Path) -> list[tuple[EvalSummary, str | None]]:
    """The newest summary per (system, split) under ``runs_dir``, with its ``cases_sha256``.

    Run directories are named ``<UTC timestamp>-<system>-<split>``, so the lexicographically
    last directory holding a ``summary.json`` wins. Sorted by (system, split).
    """
    if not runs_dir.is_dir():
        return []
    latest: dict[tuple[str, str], tuple[EvalSummary, str | None]] = {}
    for run in sorted(p for p in runs_dir.iterdir() if (p / SUMMARY_FILE).is_file()):
        raw = json.loads((run / SUMMARY_FILE).read_text(encoding="utf-8"))
        summary = EvalSummary.from_json(raw)
        latest[(summary.system, summary.split)] = (summary, raw.get("cases_sha256"))
    return [latest[k] for k in sorted(latest)]


def format_ci(ci: CI | None) -> str:
    """``(0.814, 0.79, 0.836)`` -> ``81.4% [79.0, 83.6]``; ``None`` -> ``—``."""
    if ci is None:
        return _NONE
    mean, lo, hi = ci
    return f"{mean * 100:.1f}% [{lo * 100:.1f}, {hi * 100:.1f}]"


def _presented(metric: str, ci: CI | None, n: int | None) -> CI | None:
    """``ci``, or the exact binomial interval when a per-case boolean sits at 0 or 1.

    The percentile bootstrap of an all-0 or all-1 sample has zero width, which overstates
    certainty. For the per-case boolean metrics (and fabrication, which at exactly 0 or 1 is
    0 or 1 on every case) the count is then ``0`` or ``n`` of ``n`` cases, and the interval
    shown is :func:`~sluice.evals.stats.clopper_pearson` on it. Presentation only: the summary
    keeps the bootstrap numbers.
    """
    if ci is None or metric not in _EXACT_AT_BOUNDARY or not n or ci[0] not in (0.0, 1.0):
        return ci
    mean = ci[0]
    lo, hi = clopper_pearson(round(mean * n), n)
    return (mean, lo, hi)


def _cell(metric: str, ci: CI | None, n_defined: int | None, n_row: int | None) -> str:
    """``format_ci`` plus ``(n=k)`` when the metric is defined on fewer cases than the row."""
    n = n_defined if n_defined is not None else n_row
    text = format_ci(_presented(metric, ci, n))
    if ci is not None and n_defined is not None and n_defined != n_row:
        text += f" (n={n_defined})"
    return text


def eval_command(summary: EvalSummary) -> str:
    """The shell command that reproduces ``summary``'s run, from the repo root."""
    return (
        f"{_CLI} eval --system {summary.system} --split {summary.split} --trials {summary.trials}"
    )


def _row(cells: Iterable[object]) -> str:
    return "| " + " | ".join(str(c) for c in cells) + " |"


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def _notes(s: EvalSummary) -> list[str]:
    """The per-summary sentences under the overall table (fabrication denominator, quarantines)."""
    notes: list[str] = []
    fab_n = s.n_defined.get("fabrication_rate")
    if fab_n is not None:
        notes.append(
            f"Fabrication is defined on {fab_n} of {s.n_cases} `{s.split}` cases for "
            f"`{s.system}` (those with at least one unrecoverable cell)."
        )
    if s.n_file_quarantined:
        among = (
            ""
            if fab_n is None
            else f" ({s.n_file_quarantined_fabrication_defined} of the {fab_n} cases where "
            "fabrication is defined)"
        )
        notes.append(
            f"For `{s.system}`, {s.n_file_quarantined} of {s.n_cases} `{s.split}` files were "
            f"quarantined whole{among}. A quarantined file has no output, so quarantined files "
            "contribute no fabrications (their fabrication is 0%) and fail Contract pass and "
            "Success."
        )
    return notes


def render_results(summaries: Sequence[EvalSummary], lock: Mapping[str, Any]) -> str:
    """Render ``evals/RESULTS.md`` from summaries (one per (system, split)) and ``LOCK.json``."""
    if not summaries:
        raise ValueError("no summaries to render")
    runs = "; ".join(
        f"{SYSTEM_DESCRIPTIONS.get(s.system, f'the `{s.system}` system')} on the `{s.split}` "
        f"split ({_plural(s.n_cases, 'case')}, {_plural(s.trials, 'trial')} per case)"
        for s in summaries
    )
    tail = _CLI.removeprefix("cd backend && ")
    reproduce = " && ".join(
        ["make cases", eval_command(summaries[0])]
        + [eval_command(s).removeprefix("cd backend && ") for s in summaries[1:]]
        + [f"{tail} results"]
    )
    intro = (
        f"This file reports {runs}. Each case is a drifted TPC-H table file with a known clean "
        "truth; scores come from comparing the system's output against that truth, never from "
        "human labels. **Cell accuracy** is the share of expected cells (rows x contract "
        "columns) that the output gets exactly right, where a cell that cannot be recovered "
        "counts as right only if the system flags it and leaves it empty. **Fabrication** is, "
        "among those unrecoverable cells, the share where the system wrote a value instead of "
        "flagging it (lower is better). **Row recall** is the share of expected rows present in "
        "the output. **Contract pass** is the share of cases whose output passes the table "
        "contract; a file the system quarantined whole has no output and fails it. Contract "
        "pass ignores cells the system flagged as unrecoverable, so a case can pass the "
        "contract and still fail Success. **Success** is the share of cases that are perfect: "
        "every cell right, the contract passing and no invented rows. Every value is the mean "
        "over cases with its 95% percentile-bootstrap confidence interval in brackets (10,000 "
        "resamples, seed 0), except that a Fabrication, Contract pass or Success value of "
        "exactly 0% or 100% (0 or n of n cases) shows the exact binomial (Clopper-Pearson) 95% "
        "interval instead, since the bootstrap of a constant has zero width; each case's "
        "metric is first averaged over its trials, and a case "
        "where a metric is undefined (for example fabrication when nothing needed flagging) is "
        "left out of that metric. Latency and cost are not shown. Reproduce from the repository "
        f"root with `{reproduce}`. A value followed by `(n=k)` is defined on only k of the "
        "row's cases. **Files quarantined** counts cases whose whole file the system rejected "
        "(k of n)."
    )

    headers = [h for h, _ in _TABLE_METRICS] + [_QUARANTINE_HEADER]
    main = [
        _row(["System", "Split", "n", "Trials", *headers]),
        _row(["---"] * (4 + len(headers))),
    ]
    for s in summaries:
        cells = [
            _cell(k, s.metrics.get(k), s.n_defined.get(k), s.n_cases) for _, k in _TABLE_METRICS
        ]
        cells.append(f"{s.n_file_quarantined}/{s.n_cases}")
        main.append(_row([s.system, s.split, s.n_cases, s.trials, *cells]))
    notes = [note for s in summaries for note in _notes(s)] + [
        "Contract pass checks the output against the table contract with flagged cells exempt, "
        "so a system that flags what it cannot recover can pass the contract and still fail "
        "Success, which also requires every cell to be right."
    ]

    diff = [
        _row(["System", "Split", "Difficulty", "n", *headers]),
        _row(["---"] * (4 + len(headers))),
    ]
    for s in summaries:
        for d, m in s.by_difficulty.items():
            n_d = s.n_by_difficulty.get(d)
            counts = s.n_defined_by_difficulty.get(d, {})
            cells = [_cell(k, m.get(k), counts.get(k), n_d) for _, k in _TABLE_METRICS]
            q_d = s.n_file_quarantined_by_difficulty.get(d)
            cells.append(_NONE if q_d is None or n_d is None else f"{q_d}/{n_d}")
            diff.append(_row([s.system, s.split, d, _NONE if n_d is None else n_d, *cells]))

    pass_hats = [
        f"- `{s.system}` on `{s.split}`: pass^{s.trials} = {s.pass_hat_k * 100:.1f}%"
        for s in summaries
        if s.pass_hat_k is not None
    ]

    splits = lock.get("splits", {})
    env = lock.get("environment", {})
    dates = [s.created_at for s in summaries if s.created_at is not None]
    footer = [
        "Generated by:",
        "",
        "```sh",
        reproduce,
        "```",
        "",
        *[
            f"- `{split}` cases: `LOCK.json` sha256 `{splits.get(split, {}).get('sha256', _NONE)}`"
            for split in sorted({s.split for s in summaries})
        ],
        "- Case-build environment (from `LOCK.json`): "
        + (", ".join(f"{lib} {ver}" for lib, ver in sorted(env.items())) or _NONE),
        f"- Run date (UTC): {max(dates).astimezone(UTC).date().isoformat() if dates else _NONE}",
    ]

    parts = [
        "# Results",
        "",
        intro,
        "",
        "## Overall",
        "",
        *main,
        "",
        " ".join(notes),
        "",
        "## By difficulty",
        "",
        *diff,
        "",
    ]
    if pass_hats:
        parts += ["## Consistency across trials", "", *pass_hats, ""]
    parts += ["---", "", *footer, ""]
    return "\n".join(parts)
