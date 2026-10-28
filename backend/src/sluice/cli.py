"""Command-line entry point for Sluice."""

import json
from enum import StrEnum
from pathlib import Path
from typing import Annotated

import typer

from sluice.drift.cases import build_split, load_split, split_hash

app = typer.Typer(no_args_is_help=True, help="Sluice: a self-planning agentic data pipeline.")

CASES_DIR = Path(__file__).resolve().parents[2] / "cases"
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

    ``--verify`` exits 1 when LOCK.json is missing or any split on disk differs from it.
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
        lock_path.write_text(json.dumps(hashes, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        typer.echo(f"locked {', '.join(sorted(hashes))} in {lock_path}")
    if verify:
        if not lock_path.is_file():
            typer.echo(f"missing {lock_path}", err=True)
            raise typer.Exit(1)
        locked = json.loads(lock_path.read_text(encoding="utf-8"))
        actual = _split_hashes(cases_dir)
        bad = sorted(s for s in set(locked) | set(actual) if locked.get(s) != actual.get(s))
        if bad:
            for s in bad:
                typer.echo(f"{s}: locked {locked.get(s)} != on disk {actual.get(s)}", err=True)
            raise typer.Exit(1)
        typer.echo(f"verified {', '.join(sorted(locked))} against {lock_path}")
