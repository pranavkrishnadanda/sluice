"""Command-line entry point for Sluice."""

import hashlib
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
from sluice.agent.events import (
    PIPELINE_EVENT_ADAPTER,
    EventLog,
    EventModel,
    Loaded,
    RulesApplied,
    RunFinished,
    RunStarted,
)
from sluice.contracts.model import Contract, load_contract
from sluice.drift.cases import build_split, load_reference, load_split, split_hash, unexpected_files
from sluice.evals.runner import SYSTEMS, format_ci, latest_summaries, render_results, run_eval
from sluice.evals.types import CaseInput, SystemOutput
from sluice.load.iceberg import LoadError, LoadResult, find_load, load_output
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
HASH_CHUNK_BYTES = 1 << 20
# Events the `run` command emits itself; the same types in a pipeline's meta are not replayed.
_CLI_EVENTS = frozenset({"run_started", "loaded", "run_finished"})

RunFn = Callable[[CaseInput], SystemOutput]
LoadFn = Callable[..., LoadResult]
LoadedFn = Callable[..., LoadResult | None]


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


def file_sha256(path: Path) -> str:
    """SHA-256 of ``path``'s bytes, read in 1 MiB chunks (memory O(1) in the file size)."""
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(HASH_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def resolve_run_fn(note: Callable[[str], Any]) -> RunFn:
    """The agent system's ``run``, looked up at call time; the rules system until it exists.

    The agent keeps LiteLLM's response cache (``cache=True``): a repeat ``sluice run`` should
    cost nothing, unlike an eval trial (Ruling F3).
    """
    factory = SYSTEMS.get("agent")
    if factory is None:
        note("note: the agent system is not registered yet; running the rules-only system")
        return SYSTEMS["rules"]().run
    return factory(cache=True).run  # type: ignore[call-arg]


class RunFailedError(Exception):
    """The pipeline finished ``failed``: nothing was loaded and no idempotency key was claimed."""


def _replayed(out: SystemOutput) -> tuple[list[EventModel], RunFinished | None]:
    """The pipeline's events to print (CLI-owned types dropped) and its own ``run_finished``."""
    raw = out.meta.get("events")
    if not raw:
        return [RulesApplied(actions=[str(a) for a in out.meta.get("actions", [])])], None
    events: list[EventModel] = []
    final: RunFinished | None = None
    for item in raw:
        if isinstance(item, EventModel):
            event = item
        else:
            event = PIPELINE_EVENT_ADAPTER.validate_python(item)
        if isinstance(event, RunFinished):
            final = event
        elif event.type not in _CLI_EVENTS:  # type: ignore[attr-defined]
            events.append(event)
    return events, final


def _output_status(out: SystemOutput, final: RunFinished | None) -> str:
    """The run's status: the pipeline's own, else derived from the output (rules-only system)."""
    if final is not None:
        return final.status
    return "quarantined" if out.file_quarantined or out.quarantined else "succeeded"


def _skip_summary(result: LoadResult) -> str:
    return f"file already loaded (snapshot {result.snapshot_id}); nothing written"


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

    ``run_fn`` defaults to :func:`resolve_run_fn` (agent, else rules). Before the pipeline runs,
    ``loaded_fn`` looks up the idempotency key; a file already loaded into this table is
    reported as skipped without running (or paying for) the pipeline. ``load_fn`` re-checks the
    key under its locks, so a concurrent duplicate is still caught.

    The pipeline's own final ``run_finished`` (in ``meta["events"]``) decides the outcome
    (Ruling F1): ``failed`` (a cap, an outage, the sandbox, or a crash) loads nothing and
    claims no key, prints ``run_finished(failed)`` with the pipeline's summary, tokens and
    cost, and raises :class:`RunFailedError`; any other status is passed to ``load_fn`` and
    printed as the final status. A whole-file quarantine is passed too: the loader records the
    run but writes no data and claims no key, so the file can be retried. On an exception a
    ``run_finished`` event with status ``failed`` is printed and the exception re-raised.
    """
    contract: Contract = load_contract(table, contract_version)
    run_id = uuid.uuid4().hex
    log = EventLog()

    def emit(event: EventModel) -> None:
        echo(log.emit(event).to_json())

    emit(RunStarted(run_id=run_id, file_name=path.name, table=table, rows=None))  # type: ignore[arg-type]
    cost, tokens = 0.0, 0
    try:
        sha = file_sha256(path)
        settings = settings_fn()
        prior = loaded_fn(contract, sha, settings=settings)
        if prior is not None:
            emit(
                RunFinished(
                    status="succeeded",
                    summary=_skip_summary(prior),
                    cost_usd=0.0,
                    tokens=0,
                )
            )
            return prior
        fn = run_fn or resolve_run_fn(note=lambda m: typer.echo(m, err=True))
        out = fn(CaseInput(path, contract, reference_fn(table)))
        cost = max(out.cost_usd, 0.0)
        tokens = max(int(out.meta.get("tokens", 0)), 0)
        events, final = _replayed(out)
        for event in events:
            emit(event)
        status = _output_status(out, final)
        if status == "failed":
            summary = final.summary if final is not None else "the run failed"
        else:
            result = load_fn(
                out, contract, run_id=run_id, file_sha256=sha, settings=settings, status=status
            )
    except Exception as exc:
        emit(
            RunFinished(
                status="failed", summary=f"{type(exc).__name__}", cost_usd=cost, tokens=tokens
            )
        )
        raise
    if status == "failed":
        emit(RunFinished(status="failed", summary=summary, cost_usd=cost, tokens=tokens))
        raise RunFailedError(summary)
    if not result.skipped and result.snapshot_id is not None:
        emit(
            Loaded(
                rows_loaded=result.rows_loaded,
                rows_quarantined=result.rows_quarantined,
                snapshot_id=result.snapshot_id,
            )
        )
    if result.skipped:
        summary = _skip_summary(result)
    elif out.file_quarantined:
        summary = "nothing loaded; the file can be retried"
    else:
        summary = f"loaded {result.rows_loaded} row(s), quarantined {result.rows_quarantined}" + (
            " (recovered an earlier commit)" if result.recovered else ""
        )
    if final is not None and not result.skipped:
        summary = f"{final.summary}; {summary}"
    emit(
        RunFinished(
            status="succeeded" if result.skipped else status,  # type: ignore[arg-type]
            summary=summary,
            cost_usd=cost,
            tokens=tokens,
        )
    )
    return result


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
