"""Frozen benchmark cases: seeded drift recipes turned into input / truth / manifest triples.

A case directory holds ``input.<ext>`` (the drifted file a pipeline must ingest),
``expected.parquet`` (clean truth in the contract schema) and ``manifest.json`` (what was done
and what a healer is expected to flag or quarantine). A split (``dev`` / ``holdout``) is a set of
cases built from one seed; :func:`split_hash` locks its exact bytes.

Recipe sampling guarantees every recipe applies without :class:`~sluice.drift.ops.DriftError`:
each column-targeting op owns its column (so no column ever gets two dtype-changing ops),
destroy/drop_column and value-changing ops never touch primary-key columns, ``unicode_text``
only hits free-text ``*_comment`` columns, ``case_noise`` only enum columns, ``unit_paise`` only
``INR`` money columns, and ``fk_break`` only foreign keys (lineitem).

Seeds: case ``i`` of split ``s`` built with seed ``S`` gets the per-case seed
``SeedSequence([S, SPLIT_SALT[s], i])``; its recipe is sampled from ``default_rng([seed, 1])``
and its rows and ops from ``default_rng(seed)``. ``dev`` and ``holdout`` therefore never share a
stream, even with the same ``S``.
"""

import hashlib
import json
import re
import shutil
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from functools import cache
from pathlib import Path
from typing import Any, Literal

import numpy as np
import polars as pl

from sluice.contracts.model import Contract, load_contract
from sluice.data.tpch import generate_tpch
from sluice.drift.ops import DATE_FORMATS, DESTROY_TOKENS, OPS
from sluice.drift.state import DriftState, PkTuple
from sluice.drift.writers import FORMATS, extension, write_input

Difficulty = Literal["easy", "medium", "hard"]
Split = Literal["dev", "holdout"]

TPCH_SF = 0.01
CONTRACT_VERSION = "v1"
N_ROWS = 500
TABLES: tuple[str, ...] = ("orders", "lineitem")
TABLE_WEIGHTS: tuple[int, ...] = (60, 40)  # percent, aligned with TABLES
DIFFICULTIES: tuple[str, ...] = ("easy", "medium", "hard")
DIFFICULTY_WEIGHTS: tuple[int, ...] = (30, 40, 30)  # percent, aligned with DIFFICULTIES
SPLIT_SALT: dict[str, int] = {"dev": 1, "holdout": 2}
RECIPE_STREAM = 1

FRACS: tuple[float, ...] = (0.05, 0.1, 0.2, 0.3)
CSV_LATIN1_SHARE = 0.2
CSV_UTF8_SIG_SHARE = 0.2
LATIN1 = "latin-1"
EASY_DATE_FORMATS: list[str] = ["%d/%m/%Y"]
DATE_FORMAT_CHOICES: tuple[list[str], ...] = (
    ["%d/%m/%Y"],
    ["%d %b %Y"],
    list(DATE_FORMATS),
)
ADD_COLUMN_NAMES: tuple[str, ...] = ("source_system", "ingest_batch", "loaded_by")
MONEY_UNIT = "INR"
COMMENT_SUFFIX = "_comment"

EASY_OPS: tuple[str, ...] = ("whitespace", "case_noise", "duplicate_rows", "date_format_mix")
MEDIUM_REQUIRED: tuple[str, ...] = ("rename", "indian_number_string", "tz_ist", "unit_paise")
MEDIUM_FILLERS: tuple[str, ...] = (
    "whitespace",
    "case_noise",
    "date_format_mix",
    "duplicate_rows",
    "unicode_text",
    "add_column",
)
HARD_SEVERE: tuple[str, ...] = ("unit_paise", "destroy", "drop_column", "fk_break")
HARD_FILLERS: tuple[str, ...] = (
    "rename",
    "indian_number_string",
    "unit_paise",
    "tz_ist",
    "date_format_mix",
    "whitespace",
    "case_noise",
    "destroy",
    "duplicate_rows",
    "fk_break",
    "drop_column",
    "add_column",
    "unicode_text",
)
COLUMNLESS_OPS: frozenset[str] = frozenset({"duplicate_rows", "add_column"})

MANIFEST_KEYS: tuple[str, ...] = (
    "case_id",
    "split",
    "table",
    "contract_version",
    "seed",
    "difficulty",
    "format",
    "encoding",
    "n_rows",
    "ops",
    "expected_flags",
    "expected_quarantined",
    "file_quarantine",
)
MANIFEST_FILE = "manifest.json"
EXPECTED_FILE = "expected.parquet"
INPUT_STEM = "input"
CASE_DIR_RE = re.compile(r"^[a-z]+-\d{4}$")


@dataclass(frozen=True, slots=True)
class Recipe:
    """What to do to one sample: ops (name, params) in order, output format and encoding."""

    table: str
    difficulty: Difficulty
    ops: list[tuple[str, dict[str, Any]]]
    fmt: str
    encoding: str
    n_rows: int


@dataclass(frozen=True, slots=True)
class PlannedCase:
    """One case of a split before it is built."""

    case_id: str
    seed: int
    recipe: Recipe
    severe: str | None = None


@dataclass(frozen=True, slots=True)
class Case:
    """A built case on disk."""

    case_id: str
    split: str
    table: str
    dir: Path
    manifest: dict[str, Any]

    @property
    def difficulty(self) -> str:
        """``easy``, ``medium`` or ``hard``."""
        return self.manifest["difficulty"]

    @property
    def input_path(self) -> Path:
        """The drifted input file."""
        return self.dir / f"{INPUT_STEM}.{extension(self.manifest['format'])}"

    def expected(self) -> pl.DataFrame:
        """Clean truth in the exact contract schema."""
        return pl.read_parquet(self.dir / EXPECTED_FILE)

    def flags(self) -> set[tuple[PkTuple, str]]:
        """``(clean_pk, contract_column)`` cells a healer is expected to flag."""
        return {(tuple(pk), column) for pk, column in self.manifest["expected_flags"]}

    def quarantined(self) -> set[PkTuple]:
        """Clean PKs of rows expected to land in quarantine."""
        return {tuple(pk) for pk in self.manifest["expected_quarantined"]}


# --- allocation ------------------------------------------------------------------------------


def allocate(n: int, weights: tuple[int, ...]) -> list[int]:
    """Split ``n`` into counts proportional to integer ``weights`` (largest-remainder method).

    Counts always sum to ``n``; ties in remainder go to the earlier weight.
    """
    if n < 0:
        raise ValueError(f"n must be >= 0, got {n}")
    if not weights or any(w < 0 for w in weights) or sum(weights) == 0:
        raise ValueError(f"weights must be non-negative with a positive sum, got {weights}")
    total = sum(weights)
    counts = [n * w // total for w in weights]
    remainders = [n * w % total for w in weights]
    order = sorted(range(len(weights)), key=lambda i: (-remainders[i], i))
    for i in order[: n - sum(counts)]:
        counts[i] += 1
    return counts


# --- recipe sampling -------------------------------------------------------------------------


def _pick[T](rng: np.random.Generator, items: list[T] | tuple[T, ...]) -> T:
    return items[int(rng.integers(len(items)))]


def _pk_free(contract: Contract) -> list[str]:
    return [c.name for c in contract.columns if c.name not in contract.primary_key]


def _eligible_columns(contract: Contract) -> dict[str, list[str]]:
    """Op name -> contract columns it can safely target on a clean frame."""
    cols = {c.name: c for c in contract.columns}
    non_pk = _pk_free(contract)
    return {
        "rename": list(cols),
        "indian_number_string": [
            n for n in non_pk if cols[n].dtype in ("decimal", "int64", "int32")
        ],
        "unit_paise": [
            n for n in non_pk if cols[n].dtype == "decimal" and cols[n].unit == MONEY_UNIT
        ],
        "tz_ist": [n for n in non_pk if cols[n].dtype == "timestamp_utc"],
        "date_format_mix": [n for n in non_pk if cols[n].dtype == "date"],
        "whitespace": [n for n in non_pk if cols[n].dtype == "string"],
        "case_noise": [n for n in non_pk if cols[n].enum is not None],
        "destroy": non_pk,
        "fk_break": [n for n in contract.foreign_keys if cols[n].dtype in ("int64", "int32")],
        "drop_column": non_pk,
        "unicode_text": [
            n for n in non_pk if cols[n].dtype == "string" and n.endswith(COMMENT_SUFFIX)
        ],
    }


def _rename_target(table: str, column: str, rng: np.random.Generator) -> str:
    """A realistic producer-side name for ``column`` (never ends in ``_paise``)."""
    base = column.split("_", 1)[1] if "_" in column else column
    return _pick(rng, (base, column.upper(), base.capitalize(), f"{table}_{base}"))


class _Sampler:
    """Draws ops for one recipe, giving every column-targeting op its own column."""

    def __init__(self, table: str, difficulty: str, rng: np.random.Generator) -> None:
        self.table = table
        self.difficulty = difficulty
        self.rng = rng
        self.eligible = _eligible_columns(load_contract(table, CONTRACT_VERSION))
        self.used_columns: set[str] = set()
        self.used_ops: set[str] = set()
        self.ops: list[tuple[str, dict[str, Any]]] = []

    def free_columns(self, op: str) -> list[str]:
        return [c for c in self.eligible.get(op, []) if c not in self.used_columns]

    def available(self, op: str) -> bool:
        if op in COLUMNLESS_OPS:
            return op not in self.used_ops
        return bool(self.free_columns(op))

    def add(self, op: str, *, hinted: bool | None = None) -> None:
        params: dict[str, Any] = {}
        if op not in COLUMNLESS_OPS:
            column = _pick(self.rng, self.free_columns(op))
            self.used_columns.add(column)
            params["column"] = column
        self.used_ops.add(op)
        params.update(self._params(op, params.get("column"), hinted))
        self.ops.append((op, params))

    def _params(self, op: str, column: str | None, hinted: bool | None) -> dict[str, Any]:
        rng = self.rng
        frac = {"frac": _pick(rng, FRACS)}
        builders: dict[str, Callable[[], dict[str, Any]]] = {
            "rename": lambda: {"new_name": _rename_target(self.table, str(column), rng)},
            "indian_number_string": dict,
            "unit_paise": lambda: {
                "hinted": bool(rng.integers(2)) if hinted is None else hinted,
            },
            "tz_ist": dict,
            "date_format_mix": lambda: {
                **frac,
                "formats": list(
                    EASY_DATE_FORMATS
                    if self.difficulty == "easy"
                    else _pick(rng, DATE_FORMAT_CHOICES)
                ),
            },
            "whitespace": lambda: frac,
            "case_noise": lambda: frac,
            "destroy": lambda: {**frac, "token": _pick(rng, DESTROY_TOKENS)},
            "duplicate_rows": lambda: frac,
            "fk_break": lambda: frac,
            "drop_column": dict,
            "add_column": lambda: {"name": _pick(rng, ADD_COLUMN_NAMES)},
            "unicode_text": lambda: frac,
        }
        return builders[op]()

    def add_one_of(self, ops: tuple[str, ...], *, hinted: bool | None = None) -> None:
        options = [op for op in ops if self.available(op)]
        if not options:
            raise ValueError(f"no op of {ops} applies to table {self.table!r}")
        self.add(_pick(self.rng, options), hinted=hinted)

    def shuffled(self) -> list[tuple[str, dict[str, Any]]]:
        return [self.ops[int(i)] for i in self.rng.permutation(len(self.ops))]


def _sample_format(rng: np.random.Generator, difficulty: str) -> tuple[str, str]:
    """Format and encoding; latin-1 is only drawn for medium/hard csv (easy falls to utf-8)."""
    fmt = _pick(rng, FORMATS)
    if fmt != "csv":
        return fmt, "utf-8"
    draw = float(rng.random())
    if draw < CSV_LATIN1_SHARE:
        return fmt, "utf-8" if difficulty == "easy" else "latin-1"
    if draw < CSV_LATIN1_SHARE + CSV_UTF8_SIG_SHARE:
        return fmt, "utf-8-sig"
    return fmt, "utf-8"


def severe_options(table: str) -> tuple[str, ...]:
    """The hard-case severe ops that apply to ``table`` (``fk_break`` needs a foreign key)."""
    eligible = _eligible_columns(load_contract(table, CONTRACT_VERSION))
    return tuple(op for op in HARD_SEVERE if eligible[op])


def sample_recipe(
    table: str, difficulty: str, rng: np.random.Generator, *, severe: str | None = None
) -> Recipe:
    """Draw a drift recipe for ``table`` at ``difficulty``.

    * easy: 1 op from {whitespace, case_noise, duplicate_rows, date_format_mix (``%d/%m/%Y``)}.
    * medium: 2-3 ops, at least one of {rename, indian_number_string, tz_ist,
      unit_paise(hinted=True)}; no destructive ops.
    * hard: 3-5 ops, at least one rename and at least one of {unit_paise(hinted=False),
      destroy, drop_column, fk_break (lineitem only)}.

    Format is uniform over csv/json/json_nested/parquet. A medium/hard csv is latin-1 20% of
    the time, and every latin-1 recipe carries a ``unicode_text`` op on the table's comment
    column (counted in the op budget) so the file really holds non-ASCII bytes; easy cases are
    never latin-1. csv is utf-8-sig 20% of the time, otherwise utf-8; other formats are utf-8.

    Args:
        severe: For hard recipes, force this op from :func:`severe_options` as the severe op
            (``unit_paise`` is then unhinted); ``None`` draws one at random.

    Raises:
        ValueError: Unknown table or difficulty, or ``severe`` not valid for the table/difficulty.
    """
    if table not in TABLES:
        raise ValueError(f"unknown table {table!r}; expected one of {TABLES}")
    if difficulty not in DIFFICULTIES:
        raise ValueError(f"unknown difficulty {difficulty!r}; expected one of {DIFFICULTIES}")
    if severe is not None and (difficulty != "hard" or severe not in severe_options(table)):
        raise ValueError(f"severe op {severe!r} is not valid for {difficulty} {table}")
    fmt, encoding = _sample_format(rng, difficulty)
    s = _Sampler(table, difficulty, rng)
    if encoding == LATIN1:
        s.add("unicode_text")  # first, so it reserves the comment column
    if difficulty == "easy":
        s.add_one_of(EASY_OPS)
    elif difficulty == "medium":
        n_ops = max(int(rng.integers(2, 4)), len(s.ops) + 1)
        s.add_one_of(MEDIUM_REQUIRED, hinted=True)
        while len(s.ops) < n_ops:
            s.add_one_of((*MEDIUM_REQUIRED, *MEDIUM_FILLERS), hinted=True)
    else:
        n_ops = int(rng.integers(3, 6))
        # Severe before rename: its op may have a single eligible column (o_totalprice,
        # l_orderkey) that rename would otherwise take. Rename can target any column.
        if severe is None:
            s.add_one_of(HARD_SEVERE, hinted=False)
        else:
            s.add(severe, hinted=False)
        s.add("rename")
        while len(s.ops) < n_ops:
            s.add_one_of(HARD_FILLERS)
    return Recipe(table, difficulty, s.shuffled(), fmt, encoding, N_ROWS)


# --- building --------------------------------------------------------------------------------


def _sample_rows(
    contract: Contract, clean: pl.DataFrame, n_rows: int, rng: np.random.Generator
) -> pl.DataFrame:
    """``n_rows`` random rows; lineitem takes whole orders until it has at least ``n_rows``."""
    if contract.name == "lineitem":
        sizes = clean.group_by("l_orderkey").len().sort("l_orderkey")
        order = rng.permutation(sizes.height)
        cumulative = np.cumsum(sizes["len"].to_numpy()[order])
        take = int(np.searchsorted(cumulative, n_rows)) + 1
        keys = sizes["l_orderkey"].to_numpy()[order[:take]]
        sample = clean.filter(pl.col("l_orderkey").is_in(keys.tolist()))
    else:
        if n_rows > clean.height:
            raise ValueError(f"cannot sample {n_rows} rows from {clean.height}")
        idx = np.sort(rng.choice(clean.height, size=n_rows, replace=False))
        sample = clean[idx.tolist()]
    return sample.sort(list(contract.primary_key))


def apply_recipe(recipe: Recipe, clean: Mapping[str, pl.DataFrame], seed: int) -> DriftState:
    """Sample rows from ``clean[recipe.table]`` and apply the recipe's ops, deterministically."""
    contract = load_contract(recipe.table, CONTRACT_VERSION)
    rng = np.random.default_rng(seed)
    state = DriftState.start(
        contract, _sample_rows(contract, clean[recipe.table], recipe.n_rows, rng)
    )
    for op, params in recipe.ops:
        OPS[op](state, rng, **params)
    return state


def _manifest(
    case_id: str, split: str, recipe: Recipe, seed: int, state: DriftState, n_rows: int
) -> dict[str, Any]:
    manifest = {
        "case_id": case_id,
        "split": split,
        "table": recipe.table,
        "contract_version": CONTRACT_VERSION,
        "seed": seed,
        "difficulty": recipe.difficulty,
        "format": recipe.fmt,
        "encoding": recipe.encoding,
        "n_rows": n_rows,
        "ops": [asdict(e) for e in state.entries],
        "expected_flags": [[list(pk), col] for pk, col in sorted(state.flags)],
        "expected_quarantined": [list(pk) for pk in sorted(state.quarantined)],
        "file_quarantine": False,
    }
    return manifest


def build_case(
    case_id: str,
    split: str,
    recipe: Recipe,
    clean: Mapping[str, pl.DataFrame],
    seed: int,
    out_dir: Path,
) -> Case:
    """Build one case into ``out_dir/<case_id>/``, clearing anything already in that directory.

    ``n_rows`` in the manifest is the number of clean rows sampled (lineitem may exceed
    ``recipe.n_rows`` slightly because whole orders are taken), before any duplicate rows.
    """
    state = apply_recipe(recipe, clean, seed)
    case_dir = out_dir / case_id
    if case_dir.exists():
        shutil.rmtree(case_dir)
    case_dir.mkdir(parents=True)
    pk_names = [state.current_name[k] for k in state.contract.primary_key]
    write_input(state.df, case_dir / INPUT_STEM, recipe.fmt, recipe.encoding, pk_columns=pk_names)
    state.expected.write_parquet(case_dir / EXPECTED_FILE)
    n_rows = len(set(state.row_pk))
    manifest = _manifest(case_id, split, recipe, seed, state, n_rows)
    (case_dir / MANIFEST_FILE).write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return Case(case_id, split, recipe.table, case_dir, manifest)


def _case_seed(seed: int, split: str, index: int) -> int:
    return int(np.random.SeedSequence([seed, SPLIT_SALT[split], index]).generate_state(1)[0])


Slot = tuple[str, str, str | None]  # (difficulty, table, severe op)


def _slots(n: int) -> list[Slot]:
    """Stratified case slots, all counts by largest remainder.

    Difficulty 30/40/30 over ``n``; tables 60/40 within each difficulty; for hard cases the
    severe op split evenly over :func:`severe_options` within each table.
    """
    slots: list[Slot] = []
    for difficulty, n_diff in zip(DIFFICULTIES, allocate(n, DIFFICULTY_WEIGHTS), strict=True):
        for table, n_table in zip(TABLES, allocate(n_diff, TABLE_WEIGHTS), strict=True):
            if difficulty != "hard":
                slots += [(difficulty, table, None)] * n_table
                continue
            options = severe_options(table)
            for op, k in zip(options, allocate(n_table, (1,) * len(options)), strict=True):
                slots += [(difficulty, table, op)] * k
    return slots


def plan_split(split: str, n: int, seed: int) -> list[PlannedCase]:
    """Plan ``n`` stratified cases (see :func:`_slots`), in seeded random order."""
    if split not in SPLIT_SALT:
        raise ValueError(f"unknown split {split!r}; expected one of {tuple(SPLIT_SALT)}")
    rng = np.random.default_rng([seed, SPLIT_SALT[split]])
    slots = _slots(n)
    plan = []
    for i, j in enumerate(rng.permutation(n)):
        difficulty, table, severe = slots[int(j)]
        case_seed = _case_seed(seed, split, i)
        recipe_rng = np.random.default_rng([case_seed, RECIPE_STREAM])
        recipe = sample_recipe(table, difficulty, recipe_rng, severe=severe)
        plan.append(PlannedCase(f"{split}-{i:04d}", case_seed, recipe, severe))
    return plan


@cache
def _clean_tables() -> dict[str, pl.DataFrame]:
    """TPC-H sf 0.01 frames, generated once per process."""
    with tempfile.TemporaryDirectory(prefix="sluice-tpch-") as tmp:
        paths = generate_tpch(TPCH_SF, Path(tmp))
        return {table: pl.read_parquet(path) for table, path in paths.items()}


def build_split(split: Split, n: int, seed: int, out_dir: Path) -> list[Case]:
    """Build ``n`` cases of ``split`` into ``out_dir/<split>-NNNN/`` from TPC-H sf 0.01.

    Every existing ``<split>-*`` directory in ``out_dir`` (old cases and sync-conflict copies
    such as ``dev-0001 2``) is removed first, and each case directory is cleared before it is
    written, so the split on disk is exactly what was built.
    """
    plan = plan_split(split, n, seed)
    out_dir.mkdir(parents=True, exist_ok=True)
    for child in out_dir.iterdir():
        if child.is_dir() and child.name.startswith(f"{split}-"):
            shutil.rmtree(child)
    clean = _clean_tables()
    return [build_case(p.case_id, split, p.recipe, clean, p.seed, out_dir) for p in plan]


def load_case(case_dir: Path) -> Case:
    """Load a built case from its directory."""
    manifest = json.loads((case_dir / MANIFEST_FILE).read_text(encoding="utf-8"))
    return Case(manifest["case_id"], manifest["split"], manifest["table"], case_dir, manifest)


def _is_case_dir(path: Path) -> bool:
    return path.is_dir() and CASE_DIR_RE.fullmatch(path.name) is not None


def load_split(split_dir: Path) -> list[Case]:
    """Load every ``<split>-NNNN`` case under ``split_dir`` that holds a manifest, by name."""
    if not split_dir.is_dir():
        return []
    dirs = sorted(
        p for p in split_dir.iterdir() if _is_case_dir(p) and (p / MANIFEST_FILE).is_file()
    )
    return [load_case(d) for d in dirs]


def unexpected_files(split_dir: Path) -> list[Path]:
    """Non-hidden paths under ``split_dir`` that are not part of a well-formed case, sorted.

    A case directory may hold only ``input.<ext>`` (ext from its manifest's format),
    ``expected.parquet`` and ``manifest.json``. Anything else - a stray file or directory in
    the split, a case directory without a manifest, a sync-conflict copy like
    ``manifest 2.json`` - is reported. Hidden files (``.DS_Store``) are ignored.
    """
    if not split_dir.is_dir():
        return []
    found: list[Path] = []
    for child in sorted(split_dir.iterdir()):
        if child.name.startswith("."):
            continue
        if not _is_case_dir(child) or not (child / MANIFEST_FILE).is_file():
            found.append(child)
            continue
        fmt = json.loads((child / MANIFEST_FILE).read_text(encoding="utf-8"))["format"]
        allowed = {f"{INPUT_STEM}.{extension(fmt)}", EXPECTED_FILE, MANIFEST_FILE}
        found += [
            f
            for f in sorted(child.iterdir())
            if not f.name.startswith(".") and (f.name not in allowed or not f.is_file())
        ]
    return found


def split_hash(cases: list[Case]) -> str:
    """SHA-256 over every non-hidden file of every case, in sorted ``<case_id>/<file>`` order.

    Each file contributes its relative path and length as well as its bytes, so renaming,
    moving or truncating a file changes the hash too.
    """
    files = sorted(
        (f"{case.dir.name}/{f.name}", f)
        for case in cases
        for f in case.dir.iterdir()
        if f.is_file() and not f.name.startswith(".")
    )
    digest = hashlib.sha256()
    for rel, path in files:
        data = path.read_bytes()
        digest.update(rel.encode("utf-8") + b"\0" + str(len(data)).encode("ascii") + b"\0")
        digest.update(data)
    return digest.hexdigest()


def load_reference(table: str) -> dict[str, set[Any]]:
    """Parent keys for ``table``'s foreign-key checks, from TPC-H sf 0.01.

    Returns ``{"<parent_table>.<parent_column>": {keys...}}`` (``{}`` for orders).
    """
    contract = load_contract(table, CONTRACT_VERSION)
    tables = _clean_tables()
    return {
        f"{ref_table}.{ref_col}": set(tables[ref_table][ref_col].to_list())
        for ref_table, ref_col in contract.foreign_keys.values()
    }
