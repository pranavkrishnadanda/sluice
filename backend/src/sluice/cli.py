"""Command-line entry point for Sluice."""

import json
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated

import duckdb
import polars as pl
import typer

from sluice.drift.cases import build_split, load_split, split_hash, unexpected_files
from sluice.evals.runner import SYSTEMS, format_ci, latest_summaries, render_results, run_eval

app = typer.Typer(no_args_is_help=True, help="Sluice: a self-planning agentic data pipeline.")

CASES_DIR = Path(__file__).resolve().parents[2] / "cases"
EVALS_DIR = Path(__file__).resolve().parents[3] / "evals"
RUNS_DIR = EVALS_DIR / "runs"
RESULTS_MD = EVALS_DIR / "RESULTS.md"
LOCK_FILE = "LOCK.json"
DEFAULT_SEED = 2026
DEFAULT_N = 300


class SplitName(StrEnum):
    """A benchmark split."""

    dev = "dev"
    holdout = "holdout"


@app.callback()
def main() -> None:
    """Sluice: a self-planning agentic data pipeline."""


def environment() -> dict[str, str]:
    """Versions of the libraries whose output bytes feed the split hashes."""
    return {"duckdb": duckdb.__version__, "polars": pl.__version__}


def _environment_drift(locked: object) -> str | None:
    """Human-readable version changes between ``locked`` and now, or ``None`` if unchanged."""
    locked_env = locked if isinstance(locked, dict) else {}
    now = environment()
    changes = [
        f"{lib} {locked_env.get(lib, 'unknown')} -> {version}"
        for lib, version in now.items()
        if locked_env.get(lib) != version
    ]
    return ", ".join(changes) or None


def _split_hashes(cases_dir: Path) -> dict[str, dict[str, object]]:
    out: dict[str, dict[str, object]] = {}
    for split in SplitName:
        found = load_split(cases_dir / split.value)
        if found:
            out[split.value] = {"n_cases": len(found), "sha256": split_hash(found)}
    return out


@app.command()
def cases(
    split: Annotated[
        SplitName | None, typer.Option(help="Build this split into <cases-dir>/<split>/.")
    ] = None,
    n: Annotated[int, typer.Option(min=1, help="Number of cases to build.")] = DEFAULT_N,
    seed: Annotated[int, typer.Option(help="Split seed.")] = DEFAULT_SEED,
    lock: Annotated[bool, typer.Option("--lock", help="Rewrite LOCK.json from disk.")] = False,
    verify: Annotated[
        bool, typer.Option("--verify", help="Check on-disk splits against LOCK.json.")
    ] = False,
    cases_dir: Annotated[Path, typer.Option(help="Root of the case splits.")] = CASES_DIR,
) -> None:
    """Build, lock and/or verify the frozen benchmark case splits (applied in that order).

    LOCK.json records each split's case count and hash plus the polars/duckdb versions that
    produced them. ``--verify`` exits 1 when LOCK.json is missing, the library versions changed
    (rebuild and re-lock), a case directory holds unexpected files, or a split differs.
    """
    if split is None and not lock and not verify:
        raise typer.BadParameter("pass --split, --lock and/or --verify")
    lock_path = cases_dir / LOCK_FILE
    if split is not None:
        built = build_split(split.value, n, seed, cases_dir / split.value)
        typer.echo(f"built {len(built)} {split.value} cases in {cases_dir / split.value}")
    if lock:
        hashes = _split_hashes(cases_dir)
        if not hashes:
            typer.echo(f"no cases under {cases_dir} to lock", err=True)
            raise typer.Exit(1)
        content = {"environment": environment(), "splits": hashes}
        lock_path.write_text(json.dumps(content, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        typer.echo(f"locked {', '.join(sorted(hashes))} in {lock_path}")
    if verify:
        if not lock_path.is_file():
            typer.echo(f"missing {lock_path}", err=True)
            raise typer.Exit(1)
        lock_data = json.loads(lock_path.read_text(encoding="utf-8"))
        if changed := _environment_drift(lock_data.get("environment")):
            typer.echo(
                f"environment changed ({changed}); rebuild with `make cases` and re-lock "
                "with `sluice cases --lock`",
                err=True,
            )
            raise typer.Exit(1)
        stray = [p for split in SplitName for p in unexpected_files(cases_dir / split.value)]
        if stray:
            typer.echo(f"{len(stray)} unexpected file(s) in case directories:", err=True)
            for path in stray:
                typer.echo(f"  {path}", err=True)
            raise typer.Exit(1)
        locked = lock_data.get("splits", {})
        actual = _split_hashes(cases_dir)
        bad = sorted(s for s in set(locked) | set(actual) if locked.get(s) != actual.get(s))
        if bad:
            for s in bad:
                typer.echo(f"{s}: locked {locked.get(s)} != on disk {actual.get(s)}", err=True)
            raise typer.Exit(1)
        typer.echo(f"verified {', '.join(sorted(locked))} against {lock_path}")


@app.command("eval")
def eval_cmd(
    system: Annotated[str, typer.Option(help=f"System to run: {', '.join(sorted(SYSTEMS))}.")] = (
        "rules"
    ),
    split: Annotated[SplitName, typer.Option(help="Split to evaluate.")] = SplitName.dev,
    trials: Annotated[int, typer.Option(min=1, help="Runs per case.")] = 1,
    cases_dir: Annotated[Path, typer.Option(help="Root of the case splits.")] = CASES_DIR,
    runs_dir: Annotated[Path, typer.Option(help="Where run directories are created.")] = RUNS_DIR,
    out_dir: Annotated[
        Path | None,
        typer.Option(help="Exact run directory (default: <runs-dir>/<UTC>-<system>-<split>)."),
    ] = None,
) -> None:
    """Run a system over a split; write results.jsonl and summary.json to a new run directory."""
    if system not in SYSTEMS:
        raise typer.BadParameter(
            f"unknown system {system!r}; known: {', '.join(sorted(SYSTEMS))}", param_hint="--system"
        )
    now = datetime.now(UTC).replace(microsecond=0)
    out = out_dir or runs_dir / f"{now:%Y%m%dT%H%M%SZ}-{system}-{split.value}"
    if out.exists() and any(out.iterdir()):
        typer.echo(f"{out} already exists and is not empty", err=True)
        raise typer.Exit(1)
    try:
        summary = run_eval(
            system, split.value, trials, cases_dir / split.value, out, created_at=now
        )
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    typer.echo(f"{system} on {split.value}: {summary.n_cases} cases x {trials} trial(s) -> {out}")
    for name in ("cell_accuracy", "fabrication_rate", "row_recall", "contract_pass", "success"):
        typer.echo(f"  {name}: {format_ci(summary.metrics[name])}")


@app.command()
def results(
    runs_dir: Annotated[Path, typer.Option(help="Directory of eval runs.")] = RUNS_DIR,
    lock: Annotated[Path, typer.Option(help="The cases LOCK.json.")] = CASES_DIR / LOCK_FILE,
    out: Annotated[Path, typer.Option(help="Markdown file to write.")] = RESULTS_MD,
) -> None:
    """Render RESULTS.md from the latest summary per (system, split).

    Exits 1 when there are no runs, LOCK.json is missing, or a run's cases do not match the
    split hash in LOCK.json (numbers on unlocked cases are never published).
    """
    found = latest_summaries(runs_dir)
    if not found:
        typer.echo(f"no eval runs with a summary under {runs_dir}", err=True)
        raise typer.Exit(1)
    if not lock.is_file():
        typer.echo(f"missing {lock}", err=True)
        raise typer.Exit(1)
    lock_data = json.loads(lock.read_text(encoding="utf-8"))
    splits = lock_data.get("splits", {})
    bad = [
        f"{s.system}/{s.split}: cases {sha} != locked {splits.get(s.split, {}).get('sha256')}"
        for s, sha in found
        if sha is None or splits.get(s.split, {}).get("sha256") != sha
    ]
    if bad:
        for line in bad:
            typer.echo(line, err=True)
        raise typer.Exit(1)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_results([s for s, _ in found], lock_data), encoding="utf-8")
    typer.echo(f"wrote {out} ({len(found)} run(s))")
