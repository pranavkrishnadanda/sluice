"""Command-line entry point for Sluice."""

import json
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any

import duckdb
import polars as pl
import psycopg
import pydantic_ai
import typer
from sqlalchemy.exc import SQLAlchemyError

from sluice import tracing
from sluice.agent.events import EventModel, RunFinished
from sluice.drift.cases import build_split, load_reference, load_split, split_hash, unexpected_files
from sluice.evals.runner import SYSTEMS, format_ci, latest_summaries, render_results, run_eval
from sluice.evals.types import CaseInput, SystemOutput
from sluice.load.iceberg import LoadError, LoadResult, find_load, load_output
from sluice.runs import execute
from sluice.runs.execute import LoadedFn, LoadFn, RunFn
from sluice.runs.execute import file_sha256 as file_sha256  # re-exported: `sluice run` callers
from sluice.settings import Settings, get_settings

# The CLI owns its terminal output (eval tables, JSON): no Pydantic AI first-run banner.
pydantic_ai.BANNER_ENABLED = False

app = typer.Typer(no_args_is_help=True, help="Sluice: a self-planning agentic data pipeline.")

CASES_DIR = Path(__file__).resolve().parents[2] / "cases"
EVALS_DIR = Path(__file__).resolve().parents[3] / "evals"
RUNS_DIR = EVALS_DIR / "runs"
PUBLISHED_DIR = EVALS_DIR / "published"
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
    tracing.setup_tracing()  # no-op unless OTEL_EXPORTER_OTLP_ENDPOINT is set


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
    limit: Annotated[
        int | None,
        typer.Option(min=1, help="Run only the first N cases by id (a subset, 'subset n=N')."),
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
            system, split.value, trials, cases_dir / split.value, out, created_at=now, limit=limit
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
    published_dir: Annotated[
        Path, typer.Option(help="Committed published runs (evals/published/).")
    ] = PUBLISHED_DIR,
    lock: Annotated[Path, typer.Option(help="The cases LOCK.json.")] = CASES_DIR / LOCK_FILE,
    out: Annotated[Path, typer.Option(help="Markdown file to write.")] = RESULTS_MD,
) -> None:
    """Render RESULTS.md from the latest summary per (system, split, limit).

    Rows come from ``--runs-dir`` and ``--published-dir``; a published summary wins for its
    key except the full-split rules row, which is reproduced fresh (see
    ``sluice.evals.runner.latest_summaries``). Exits 1 when there are no runs, LOCK.json is
    missing, or a run's cases do not match the split hash in LOCK.json (numbers on unlocked
    cases are never published).
    """
    found = latest_summaries(runs_dir, published_dir)
    if not found:
        typer.echo(f"no eval runs with a summary under {runs_dir} or {published_dir}", err=True)
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


class TableName(StrEnum):
    """A table `sluice run` can load."""

    orders = "orders"
    lineitem = "lineitem"


def resolve_run_fn(note: Callable[[str], Any]) -> RunFn:
    """The agent system's ``run``, looked up at call time; the rules system until it exists.

    The agent keeps LiteLLM's response cache (``cache=True``): a repeat ``sluice run`` should
    cost nothing, unlike an eval trial (Ruling F3).
    """
    return execute.resolve_run_fn(note, SYSTEMS)


class RunFailedError(Exception):
    """The pipeline finished ``failed``: nothing was loaded and no idempotency key was claimed."""


def run_file(
    path: Path,
    table: str,
    *,
    contract_version: str = "v1",
    run_fn: RunFn | None = None,
    load_fn: LoadFn = load_output,
    loaded_fn: LoadedFn = find_load,
    settings_fn: Callable[[], Settings] = get_settings,
    reference_fn: Callable[[str], dict[str, set[Any]]] = load_reference,
    echo: Callable[[str], Any] = typer.echo,
) -> LoadResult:
    """Run the pipeline over ``path``, print its events as JSON lines, and load the output.

    A thin wrapper over :func:`sluice.runs.execute.execute_run` (see there for the event order
    and the load semantics, Rulings F1/L1/C1-C4c) with a fresh run id and each event printed as
    one JSON line as it happens. ``run_fn`` defaults to :func:`resolve_run_fn` (agent, else
    rules), resolved only once the file is known not to be loaded already.

    Raises:
        RunFailedError: The pipeline finished ``failed`` (``run_finished(failed)`` printed with
            its summary, tokens and cost; nothing loaded, no key claimed).
        Exception: Whatever the pipeline or the load raised, after ``run_finished(failed)``.
    """
    last: list[EventModel] = []

    def emit(event: EventModel) -> None:
        last[:] = [event]
        echo(event.to_json())

    def resolved(case: CaseInput) -> SystemOutput:
        return resolve_run_fn(note=lambda m: typer.echo(m, err=True))(case)

    outcome = execute.execute_run(
        path,
        table,
        run_id=uuid.uuid4().hex,
        emit=emit,
        contract_version=contract_version,
        run_fn=run_fn or resolved,
        load_fn=load_fn,
        loaded_fn=loaded_fn,
        settings_fn=settings_fn,
        reference_fn=reference_fn,
    )
    if outcome.status == "failed" or outcome.result is None:
        final = last[0] if last else None
        raise RunFailedError(final.summary if isinstance(final, RunFinished) else "the run failed")
    return outcome.result


@app.command("run")
def run_cmd(
    file: Annotated[
        Path, typer.Argument(exists=True, dir_okay=False, readable=True, help="Input file.")
    ],
    table: Annotated[TableName, typer.Option(help="Target table.")],
    contract_version: Annotated[str, typer.Option(help="Contract version.")] = "v1",
) -> None:
    """Run the pipeline over FILE, print its events as JSON lines, and load it into Iceberg.

    Loading is idempotent per (file SHA-256, contract version): a repeat writes nothing.
    """
    try:
        run_file(file, table.value, contract_version=contract_version)
    except RunFailedError as exc:
        typer.echo(f"run failed: {exc}", err=True)
        raise typer.Exit(1) from exc
    except (psycopg.Error, SQLAlchemyError) as exc:
        # Driver messages can echo connection details; print the error type only.
        typer.echo(f"run failed: {type(exc).__name__}", err=True)
        raise typer.Exit(1) from exc
    except (LoadError, ValueError, OSError) as exc:
        typer.echo(f"run failed: {type(exc).__name__}: {exc}", err=True)
        raise typer.Exit(1) from exc
