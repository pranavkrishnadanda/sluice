# Sluice v1 · Plan 1 — Foundations (first numbers) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Generate ground-truth drift cases from TPC-H, grade any system's output against them with code, and publish the first real numbers: the rules-only baseline on the `dev` split with 95% CIs.

**Architecture:** A pure-Python package (`backend/src/sluice`) with no services yet. DuckDB generates TPC-H; a seeded drift generator writes each case as `input.<ext>` + `expected.parquet` + `manifest.json`; YAML data contracts are executed through Pandera (Polars); graders align output to expected by primary key; an eval runner runs any `System` over a split and writes results + a summary with bootstrap CIs.

**Tech Stack:** Python (uv, Ruff, pytest), DuckDB, Polars, Pandera (Polars backend), PyArrow, NumPy, SciPy, Typer, PyYAML.

**Spec:** `docs/superpowers/specs/2026-10-28-sluice-design.md` (§7.1, §7.2, §7.3 steps 1–2, §7.7, §9 v1 items 2–3, §11 item 7).

**Later v1 plans (written after this one ships):** P2 agent pipeline (Compose stack, LiteLLM + models, sandbox, orchestrator/workers, Iceberg + lineage) · P3 evals at scale (hostile set, CI eval gate, routing matrix + cost–quality chart) · P4 UI + SSE API + traces + README.

## Global Constraints

- Python deps via **uv** only; `uv add <pkg>` (no version pins typed from memory — uv resolves latest stable; commit `uv.lock`). Use the newest Python version that every dependency supports (check at Task 1; prefer 3.14, else 3.13). Set `requires-python` accordingly.
- Lint/format with **Ruff**; tests with **pytest**. Line length 100.
- Every GitHub Action pinned to its latest major, verified at Task 1 (e.g. `actions/checkout`, `astral-sh/setup-uv`).
- Commits: owner is sole author; **no `Co-Authored-By` or any AI attribution** in commit messages or PRs. Conventional Commits style (`feat:`, `test:`, `chore:`, `docs:`).
- Never read, print or commit `.env`. (No secrets are needed in this plan.)
- Do not use host port 5432 or 8000 (owner's native Postgres and another process live there). Not relevant until P2, but don't introduce them.
- No metric may depend on human labels (spec §10). All graders are code.
- Money columns are `Decimal(15,2)`; graders compare money exactly.
- Everything deterministic from a seed. Same seed ⇒ byte-identical case files.

## Review Focus

1. **Header-only / zero-row input file** → `read_input` returns a 0-row frame with the header's columns; graders return `None` (not a crash, not 0.0) for any metric whose denominator is 0, and aggregates skip `None`. Pinned in Task 2 and Task 7.
2. **Duplicate column names after drift** (e.g. a rename collides with an added column) → `read_input` must not raise; duplicates get Polars' `_duplicated_N` suffix. Pinned in Task 2.
3. **Non-UTF-8 bytes in the header row** (latin-1 file) → `read_input` falls back to latin-1 and decodes `é` correctly. Pinned in Task 2.
4. **System outputs zero rows** (everything quarantined) → `row_precision` is `None`, `row_recall` is 0.0, `cell_accuracy` is 0.0. Pinned in Task 7.
5. **Paise values on the largest orders** (466001.28 rupees → 46600128 paise) → round-trip back to `Decimal("466001.28")` exactly, no float drift. Pinned in Task 4.

---

## File Structure

```
backend/
  pyproject.toml                 # package "sluice", console script `sluice`
  src/sluice/
    __init__.py                  # __version__
    io.py                        # read_input()
    data/tpch.py                 # generate_tpch()
    contracts/model.py           # ColumnSpec, Contract, load_contract()
    contracts/validate.py        # Violation, ValidationReport, validate()
    drift/state.py               # DriftState, ManifestEntry
    drift/ops.py                 # one function per drift op + OPS registry
    drift/writers.py             # write_input()
    drift/cases.py               # Case, Recipe, sample_recipe(), build_case(), build_split(), load_case()
    rules/profile.py             # Profile, profile()
    rules/fixers.py              # RuleResult, apply_rules()
    evals/types.py               # CaseInput, SystemOutput, System protocol, PK
    evals/graders.py             # Grade, grade()
    evals/stats.py               # bootstrap_ci, pass_at_k, pass_hat_k, mcnemar_p
    evals/runner.py              # EvalSummary, run_eval()
    systems/rules_only.py        # RulesOnlySystem
    cli.py                       # Typer app: `sluice cases`, `sluice eval`, `sluice results`
  contracts/orders.v1.yaml
  contracts/lineitem.v1.yaml
  cases/LOCK.json                # per-split sha256 (cases themselves are regenerated, git-ignored)
  tests/...                      # mirrors src layout
evals/RESULTS.md                 # generated results table (committed)
Makefile
.github/workflows/ci.yml
```

---

### Task 1: Python project scaffold + CI

**Files:**
- Create: `backend/pyproject.toml`, `backend/src/sluice/__init__.py`, `backend/src/sluice/cli.py` (stub), `backend/tests/test_smoke.py`, `Makefile`, `.github/workflows/ci.yml`
- Modify: `.gitignore` (add `backend/cases/*/`, `evals/runs/`, `.venv/`, `__pycache__/`, `.pytest_cache/`, `.ruff_cache/`)

**Interfaces:**
- Produces: importable package `sluice` with `__version__ = "0.1.0"`; `make test`, `make lint`; console script `sluice` → `sluice.cli:app` (a Typer app with no commands yet; commands land in Tasks 5 and 9).

- [ ] **Step 1:** `cd backend && uv init --package --name sluice` (src layout), then `uv add duckdb polars pandera pyarrow numpy scipy typer pyyaml` and `uv add --dev pytest ruff`. If Pandera's Polars backend needs an extra (`pandera[polars]`), use it.
- [ ] **Step 2: Write the failing test** `tests/test_smoke.py`:

```python
import sluice

def test_version():
    assert sluice.__version__ == "0.1.0"
```

- [ ] **Step 3:** Run `uv run pytest -q` → FAIL if `__version__` missing; add it; rerun → PASS.
- [ ] **Step 4:** Ruff config in `pyproject.toml`: `line-length = 100`, rules `E,F,I,UP,B,SIM`. pytest config: `testpaths = ["tests"]`, marker `slow`.
- [ ] **Step 5:** `Makefile` targets: `test` (`cd backend && uv run pytest -q -m "not slow"`), `test-all`, `lint` (`ruff check` + `ruff format --check`), `fmt`.
- [ ] **Step 6:** `.github/workflows/ci.yml`: on `push` and `pull_request`; ubuntu-latest; checkout → setup-uv (latest major, with cache) → `uv sync --locked` in `backend` → `make lint` → `make test`.
- [ ] **Step 7:** `make lint && make test` → both pass.
- [ ] **Step 8: Commit** `chore: scaffold sluice python package and CI`

---

### Task 2: Robust input reader

**Files:**
- Create: `backend/src/sluice/io.py`, `backend/tests/test_io.py`

**Interfaces:**
- Produces: `read_input(path: Path) -> pl.DataFrame` — format by extension (`.csv`, `.json`, `.parquet`); **all CSV/JSON columns read as `pl.String`** (no type inference — typing is the pipeline's job); JSON is an array of flat or nested objects — nested objects are flattened with `.`-joined keys (`{"details": {"o_clerk": ..}}` → column `details.o_clerk`); CSV decode tries `utf-8-sig`, then falls back to `latin-1`.

- [ ] **Step 1: Write failing tests:**

```python
def test_csv_all_strings(tmp_path):        # header "a,b", row "1,2" -> dtypes [String, String]
def test_header_only_csv(tmp_path):        # "a,b\n" -> shape (0, 2), columns ["a","b"]
def test_duplicate_header(tmp_path):       # "a,a\n1,2\n" -> no raise; 2 columns; second startswith "a_duplicated"
def test_latin1_header(tmp_path):          # bytes b"caf\xe9\n1\n" -> columns == ["café"]
def test_utf8_bom(tmp_path):               # b"\xef\xbb\xbfa\n1\n" -> columns == ["a"]
def test_nested_json(tmp_path):            # [{"k":1,"details":{"x":"y"}}] -> columns {"k","details.x"}; values "1","y"
def test_parquet_passthrough(tmp_path):    # parquet with Int64 column -> dtype preserved
```

- [ ] **Step 2:** `uv run pytest tests/test_io.py -q` → FAIL (module missing).
- [ ] **Step 3:** Implement `read_input`. JSON: `json.load` + recursive flatten, values stringified (`None` stays null), then `pl.DataFrame` with an all-`pl.String` schema.
- [ ] **Step 4:** Tests → PASS.
- [ ] **Step 5: Commit** `feat: add format- and encoding-tolerant input reader`

---

### Task 3: TPC-H generation and data contracts

**Files:**
- Create: `backend/src/sluice/data/tpch.py`, `backend/src/sluice/contracts/model.py`, `backend/src/sluice/contracts/validate.py`, `backend/contracts/orders.v1.yaml`, `backend/contracts/lineitem.v1.yaml`, tests `tests/data/test_tpch.py`, `tests/contracts/test_contracts.py`

**Interfaces:**
- Produces:
  - `generate_tpch(sf: float, out_dir: Path) -> dict[str, Path]` — writes `orders.parquet`, `lineitem.parquet`; adds derived column `o_ordertime` (`Datetime("us","UTC")`) = `o_orderdate` at 00:00 UTC + `(o_orderkey * 7919) % 86400` seconds (deterministic). Returns table → path.
  - `ColumnSpec(name: str, dtype: Literal["int64","int32","decimal","string","date","timestamp_utc"], nullable: bool = False, unit: str | None = None, enum: list[str] | None = None, min: float | None = None, max: float | None = None)` (frozen dataclass).
  - `Contract(name: str, version: str, primary_key: tuple[str, ...], columns: tuple[ColumnSpec, ...], foreign_keys: dict[str, tuple[str, str]], locale: str)`; `Contract.column(name) -> ColumnSpec`; `Contract.polars_schema() -> dict[str, pl.DataType]` (`decimal` → `pl.Decimal(15, 2)`).
  - `load_contract(name: str, version: str = "v1") -> Contract` reading `backend/contracts/<name>.<version>.yaml`.
  - `Violation(column: str | None, check: str, pk_values: list[tuple], message: str)`; `ValidationReport(passed: bool, violations: list[Violation])`.
  - `validate(contract: Contract, df: pl.DataFrame, reference: dict[str, set] | None = None) -> ValidationReport` — checks, with these exact `check` names: `missing_column`, `extra_column`, `dtype`, `not_null`, `enum`, `min`, `max`, `unique_pk`, `foreign_key` (FK checked against `reference["<table>.<col>"]` when given). `message` names the column, the check and up to 5 offending PK values.

Contract values (exact):

| orders column | dtype | extra |
|---|---|---|
| o_orderkey | int64 | PK |
| o_custkey | int64 | |
| o_orderstatus | string | enum F, O, P |
| o_totalprice | decimal | unit `INR`, min 0, max 600000 |
| o_orderdate | date | |
| o_ordertime | timestamp_utc | |
| o_orderpriority | string | enum 1-URGENT, 2-HIGH, 3-MEDIUM, 4-NOT SPECIFIED, 5-LOW |
| o_clerk | string | |
| o_shippriority | int32 | |
| o_comment | string | |

`orders.locale = "en_IN"` (dates written day-first). `lineitem`: PK (`l_orderkey`, `l_linenumber`); FK `l_orderkey → orders.o_orderkey`; columns as DuckDB emits them (BIGINT→int64, INTEGER→int32, DECIMAL(15,2)→decimal, DATE→date, VARCHAR→string); `l_returnflag` enum A, N, R; `l_linestatus` enum F, O; `l_extendedprice` unit `INR`, min 0.

- [ ] **Step 1: Write failing tests:**

```python
def test_tpch_counts(tmp_path):
    paths = generate_tpch(0.01, tmp_path)
    assert pl.read_parquet(paths["orders"]).height == 15000
    assert pl.read_parquet(paths["lineitem"]).height == 60175

def test_tpch_deterministic(tmp_path):     # two runs -> identical sha256 of orders.parquet
def test_ordertime_derived(tmp_path):      # dtype Datetime(UTC); date part == o_orderdate for every row
def test_clean_orders_pass(tmp_path):      # validate(load_contract("orders"), orders_df).passed is True
def test_negative_price_fails(tmp_path):   # one o_totalprice = -1 -> passed False; violation column "o_totalprice", check "min", that row's pk
def test_missing_column_fails(tmp_path):   # drop o_clerk -> violation check "missing_column"
def test_extra_column_fails(tmp_path):     # add "junk" -> violation check "extra_column"
def test_fk_violation(tmp_path):           # lineitem l_orderkey=-5 with reference={"orders.o_orderkey": {...}} -> check "foreign_key", pk_values contains (-5, <linenumber>)
```

- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implement. DuckDB: `INSTALL tpch; LOAD tpch; CALL dbgen(sf=...)`, export via `.pl()`, add `o_ordertime`, write Parquet. `validate` builds a `pandera.polars.DataFrameSchema` from the contract for dtype/null/enum/range/unique checks, and handles the column-set and FK checks itself.
- [ ] **Step 4:** Tests → PASS. (Counts are verified against DuckDB 2.0.0 `dbgen(sf=0.01)`: orders 15000, lineitem 60175; max `o_totalprice` 466001.28.)
- [ ] **Step 5: Commit** `feat: add TPC-H generation and executable data contracts`

---

### Task 4: Drift ops

**Files:**
- Create: `backend/src/sluice/drift/state.py`, `backend/src/sluice/drift/ops.py`, `backend/tests/drift/test_ops.py`

**Interfaces:**
- Consumes: `Contract`, `ColumnSpec` (Task 3).
- Produces:
  - `ManifestEntry(op: str, params: dict, columns: list[str], rows_affected: int)`.
  - `DriftState` (mutable dataclass): `contract: Contract`, `df: pl.DataFrame` (drifted, evolves), `expected: pl.DataFrame` (clean truth, contract schema), `flags: set[tuple[tuple, str]]` (expected-flagged `(pk, contract_column)`), `quarantined: set[tuple]` (PKs expected in quarantine), `current_name: dict[str, str]` (contract column → its current name in `df`), `entries: list[ManifestEntry]`. Constructor `DriftState.start(contract: Contract, clean_df: pl.DataFrame) -> DriftState`.
  - Every op: `def <op>(state: DriftState, rng: np.random.Generator, **params) -> None`; appends exactly one `ManifestEntry`. `OPS: dict[str, Callable]` registry keyed by op name.

Op semantics (exact):

| op | params | drifted `df` change | expected / flags / quarantine |
|---|---|---|---|
| `rename` | `column, new_name` | rename current column | none |
| `indian_number_string` | `column` | decimal → string with Indian grouping, `124500.50` → `"1,24,500.50"` | none |
| `unit_paise` | `column, hinted: bool` | whole column ×100 as int; if `hinted`, rename to `<current>_paise` | none |
| `tz_ist` | `column` | UTC → string `"YYYY-MM-DD HH:MM:SS+05:30"` in IST | none |
| `date_format_mix` | `column, frac, formats` | `frac` of rows rewritten in a format drawn from `formats` (subset of `%d/%m/%Y`, `%d %b %Y`); rest ISO strings | none |
| `whitespace` | `column, frac` | pad 1–3 spaces both sides | none |
| `case_noise` | `column, frac` | lower-case (enum columns only) | none |
| `destroy` | `column, frac, token` (`"N/A"`, `""`, `"-1"`, `"###"`) | replace cells with `token` | `(pk, column)` added to `flags`; expected cell → null |
| `duplicate_rows` | `frac` | append exact copies of `frac` of rows | none |
| `fk_break` | `column, frac` | FK set to negative ints absent from parent | PKs added to `quarantined`; rows removed from `expected` |
| `drop_column` | `column` | drop it | every `(pk, column)` added to `flags`; expected column → null |
| `add_column` | `name` | add string column of random words | none |
| `unicode_text` | `column, frac` | replace one ASCII letter with é/ñ/ü in `frac` of cells | **same change applied to `expected`** (content, not drift) |

Deferred from the spec's §7.1 drift table to P3 (they need extra ground-truth design): column split/merge and near-duplicate rows.

Ops after a `rename` address the column through `current_name`. PK tuples always use the **clean** PK values.

- [ ] **Step 1: Write failing tests** (fixture: `generate_tpch(0.01)` orders `.head(20)`, session-scoped):

```python
def test_rename_tracks_current_name():   # current_name["o_totalprice"] == "amt"; entry.rows_affected == 20
def test_indian_number_string():         # Decimal("124500.50") -> "1,24,500.50"; Decimal("999.00") -> "999.00"
def test_paise_roundtrip_exact():        # 466001.28 -> 46600128; Decimal(46600128) / 100 == Decimal("466001.28")
def test_tz_ist_offset():                # 2026-10-03 18:45:00 UTC -> "2026-10-04 00:15:00+05:30"
def test_destroy_sets_flags():           # len(flags) == rows_affected; those expected cells are null
def test_fk_break_quarantines():         # lineitem: quarantined PKs absent from expected
def test_drop_column_flags_all():        # flags == {(pk, "o_clerk") for every pk}
def test_unicode_text_changes_expected():# expected and df agree on changed cells
def test_ops_deterministic():            # same seed -> identical df and entries
```

- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implement. Indian grouping: last 3 integer digits, then groups of 2 leftwards.
- [ ] **Step 4:** Tests → PASS.
- [ ] **Step 5: Commit** `feat: add seeded drift ops with ground-truth bookkeeping`

---

### Task 5: Writers and case builder

**Files:**
- Create: `backend/src/sluice/drift/writers.py`, `backend/src/sluice/drift/cases.py`, `backend/tests/drift/test_cases.py`, `backend/cases/LOCK.json`
- Modify: `backend/src/sluice/cli.py` (add `sluice cases`)

**Interfaces:**
- Consumes: `DriftState`, `OPS` (Task 4); `generate_tpch`, `load_contract` (Task 3); `read_input` (Task 2).
- Produces:
  - `write_input(df: pl.DataFrame, path_stem: Path, fmt: Literal["csv","json","json_nested","parquet"], encoding: Literal["utf-8","utf-8-sig","latin-1"]) -> Path` — `json_nested` keeps the PK columns at top level and nests every other column under `"details"`; parquet ignores `encoding`.
  - `Recipe(table: str, difficulty: Literal["easy","medium","hard"], ops: list[tuple[str, dict]], fmt: str, encoding: str, n_rows: int)`.
  - `sample_recipe(table: str, difficulty: str, rng: np.random.Generator) -> Recipe` — **easy**: 1 op from {whitespace, case_noise, duplicate_rows, date_format_mix with `formats=["%d/%m/%Y"]`}; **medium**: 2–3 ops, at least one of {rename, indian_number_string, tz_ist, unit_paise(hinted=True)}; **hard**: 3–5 ops, at least one rename and at least one of {unit_paise(hinted=False), destroy, drop_column, fk_break (lineitem only)}. Format uniform over the 4 formats; latin-1 only with csv, on 20% of csv cases; `n_rows = 500`.
  - `Case(case_id: str, split: str, table: str, dir: Path, manifest: dict)`; properties/methods `input_path: Path`, `expected() -> pl.DataFrame`, `flags() -> set[tuple[tuple, str]]`, `quarantined() -> set[tuple]`, `difficulty: str`.
  - `build_case(case_id: str, split: str, recipe: Recipe, clean: dict[str, pl.DataFrame], seed: int, out_dir: Path) -> Case` — samples `n_rows` rows (lineitem: sample whole orders so line numbers stay complete), applies ops in order, writes `input.<ext>`, `expected.parquet`, `manifest.json`.
  - `build_split(split: Literal["dev","holdout"], n: int, seed: int, out_dir: Path) -> list[Case]` — difficulty mix allocated by count 30% easy / 40% medium / 30% hard; tables 60% orders / 40% lineitem; ids `<split>-<0000>`; uses TPC-H sf 0.01.
  - `load_case(dir: Path) -> Case`; `split_hash(cases: list[Case]) -> str` (sha256 over the bytes of every case file, in sorted path order).
  - `load_reference(table: str) -> dict[str, set]` — parent keys for FK checks (`{"orders.o_orderkey": {...}}` for lineitem; `{}` for orders), from the same sf 0.01 TPC-H.

`manifest.json` keys (exact): `case_id, split, table, contract_version, seed, difficulty, format, encoding, n_rows, ops, expected_flags, expected_quarantined, file_quarantine` — `ops` is a list of `ManifestEntry` dicts, `expected_flags` is `[[pk_list, column], ...]`, `expected_quarantined` is `[pk_list, ...]`, `file_quarantine` is always `false` in v1.

- [ ] **Step 1: Write failing tests:**

```python
def test_build_case_files(tmp_path):          # input.*, expected.parquet, manifest.json exist; manifest keys exactly as specified
def test_case_roundtrip(tmp_path):            # load_case(dir).flags() == state.flags
def test_split_deterministic(tmp_path):       # build_split("dev", 5, 1, a) and (..., b) -> equal split_hash
def test_dev_holdout_disjoint(tmp_path):      # n=5 each -> no identical expected.parquet bytes across splits
def test_difficulty_mix():                    # n=100 -> exactly 30 easy, 40 medium, 30 hard
def test_latin1_written_and_readable(tmp_path)  # latin-1 csv containing é round-trips through read_input
```

- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implement. CLI: `sluice cases --split dev --n 300` / `--split holdout --n 300` write to `backend/cases/<split>/`; `sluice cases --verify` recomputes hashes and compares with `backend/cases/LOCK.json` (exit 1 on mismatch); `--lock` rewrites `LOCK.json`.
- [ ] **Step 4:** Tests → PASS. Generate both splits (300 each), `sluice cases --lock`, then `sluice cases --verify` → exit 0. Add `make cases` = generate both + verify.
- [ ] **Step 5: Commit** `feat: add drift case builder with frozen dev/holdout splits` (commit `LOCK.json`; case dirs stay git-ignored).

---

### Task 6: Profiler and rule fixers (rules-only system)

**Files:**
- Create: `backend/src/sluice/evals/types.py`, `backend/src/sluice/rules/profile.py`, `backend/src/sluice/rules/fixers.py`, `backend/src/sluice/systems/rules_only.py`, `backend/tests/rules/test_rules.py`

**Interfaces:**
- Consumes: `read_input`, `Contract`, `validate`, `load_reference`.
- Produces:
  - `PK = tuple[Any, ...]`.
  - `CaseInput(path: Path, contract: Contract, reference: dict[str, set])`.
  - `SystemOutput(df: pl.DataFrame, flags: set[tuple[PK, str]], quarantined: set[PK], file_quarantined: bool, cost_usd: float, latency_s: float, meta: dict)`.
  - `class System(Protocol): name: str; def run(self, case: CaseInput) -> SystemOutput: ...`
  - `ColumnProfile(name: str, null_frac: float, distinct: int, sample: list[str], matched_contract_column: str | None)`; `Profile(columns: dict[str, ColumnProfile], missing: list[str], extra: list[str], n_rows: int)`; `profile(df: pl.DataFrame, contract: Contract) -> Profile`. Matching: exact, else case-insensitive after strip and `-`/space → `_`. **No fuzzy matching** (that is the agent's job in P2).
  - `RuleResult(df: pl.DataFrame, flags: set[tuple[PK, str]], quarantined: set[PK], actions: list[str])`; `apply_rules(df: pl.DataFrame, contract: Contract, prof: Profile, reference: dict[str, set]) -> RuleResult`.
  - `RulesOnlySystem` with `name = "rules"`, implementing `System`.

Rule order (exact): normalise matched names → drop extra columns → strip whitespace on string cells → upper-case enum cells when that makes them valid → sentinel tokens `"N/A"`, `""`, `"###"` in non-nullable columns → null + flag → parse ISO dates/timestamps and `%d/%m/%Y` (contract locale `en_IN`) → lossless casts to contract dtypes (money: plain decimal strings only; **no** Indian-comma or paise handling) → any cell still failing its cast → null + flag → missing contract columns added as null and flagged for every row → exact duplicate rows dropped, then duplicate PKs keep first → FK misses moved to `quarantined` and removed from `df`.

- [ ] **Step 1: Write failing tests:**

```python
def test_rules_fix_whitespace_and_case():      # " o " in o_orderstatus -> "O"
def test_rules_parse_dmy():                    # "03/10/2026" -> date(2026, 10, 3)
def test_rules_dont_guess_indian_numbers():    # "1,24,500.50" -> null + flagged, NOT 124500.50
def test_rules_flag_missing_column():          # dropped o_clerk -> flagged for every row
def test_rules_dedupe():                       # output height == number of unique PKs
def test_rules_fk_quarantine():                # lineitem l_orderkey -5 -> pk in quarantined, not in df
def test_rules_easy_case_passes_contract(tmp_path)  # a built easy case -> validate(output).passed
```

- [ ] **Step 2:** Run → FAIL. **Step 3:** Implement. **Step 4:** Tests → PASS.
- [ ] **Step 5: Commit** `feat: add profiler and rules-only baseline system`

---

### Task 7: Graders

**Files:**
- Create: `backend/src/sluice/evals/graders.py`, `backend/tests/evals/test_graders.py`

**Interfaces:**
- Consumes: `Case` (Task 5); `SystemOutput`, `PK` (Task 6); `validate` (Task 3).
- Produces: `Grade(case_id: str, difficulty: str, cell_accuracy: float | None, fabrication_rate: float | None, fabricated_rows: int, extra_columns: list[str], row_recall: float | None, row_precision: float | None, contract_pass: bool, success: bool, n_cells: int, n_flag_cells: int)`; `grade(case: Case, out: SystemOutput, reference: dict[str, set]) -> Grade`.

Definitions (exact; spec §7.7 and §11 item 7). E = expected rows by PK; O = output rows by PK (O = ∅ if `out.file_quarantined`).
- `row_recall = |PK(O) ∩ PK(E)| / |PK(E)|`; `row_precision = |PK(O) ∩ PK(E)| / |PK(O)|`; `None` when the denominator is 0.
- For each pk ∈ E and contract column c: **truth** = `FLAG` if `(pk, c)` ∈ `case.flags()` else the expected value. **Prediction** = `MISSING` if pk ∉ O; `FLAG` if `(pk, c)` ∈ `out.flags` and the output cell is null; otherwise the output value (null counts as a value of null).
- Correct ⇔ truth == prediction (FLAG == FLAG; values compared exactly after casting output to contract dtypes; timestamps to the second). `cell_accuracy = correct / n_cells`, `None` if `n_cells == 0`.
- `fabrication_rate = |{truth = FLAG ∧ prediction is a non-null value}| / n_flag_cells`; `None` if `n_flag_cells == 0`.
- `fabricated_rows = |PK(O) \ (PK(E) ∪ case.quarantined())|`; `extra_columns` = output columns not in contract.
- `contract_pass = validate(contract, out.df, reference).passed`.
- `success = cell_accuracy == 1.0 and contract_pass and fabricated_rows == 0`.

- [ ] **Step 1: Write failing tests** with hand-built 3-row frames:

```python
def test_perfect_output():         # cell_accuracy 1.0, fabrication_rate None, success True
def test_flag_correct():           # truth FLAG, out flags it with null -> correct; fabrication_rate 0.0
def test_fabrication():            # truth FLAG, out puts a value -> fabrication_rate 1.0; cell_accuracy < 1
def test_flag_when_recoverable():  # out flags a recoverable cell -> accuracy drops; fabrication_rate unaffected
def test_missing_row():            # one expected row absent -> row_recall == 2/3; its cells incorrect
def test_zero_output_rows():       # O empty -> row_precision None, row_recall 0.0, cell_accuracy 0.0
def test_zero_expected_rows():     # E empty -> cell_accuracy None, row_recall None
def test_invented_row():           # PK not in input -> fabricated_rows 1, success False
```

- [ ] **Step 2:** Run → FAIL. **Step 3:** Implement. **Step 4:** Tests → PASS.
- [ ] **Step 5: Commit** `feat: add ground-truth graders`

---

### Task 8: Statistics

**Files:**
- Create: `backend/src/sluice/evals/stats.py`, `backend/tests/evals/test_stats.py`

**Interfaces:**
- Produces:
  - `bootstrap_ci(values: Sequence[float | None], n_resamples: int = 10_000, alpha: float = 0.05, seed: int = 0) -> tuple[float, float, float] | None` — drops `None`; percentile method; `(mean, lo, hi)`; `None` if no values remain.
  - `pass_at_k(n: int, c: int, k: int) -> float` = `1 - C(n-c, k) / C(n, k)`.
  - `pass_hat_k(n: int, c: int, k: int) -> float` = `C(c, k) / C(n, k)` (τ-bench pass^k).
  - `mcnemar_p(a: Sequence[bool], b: Sequence[bool]) -> float` — exact two-sided binomial test on discordant pairs (`scipy.stats.binomtest`, p = 0.5); 1.0 if no discordant pairs.

- [ ] **Step 1: Write failing tests:**

```python
def test_bootstrap_constant(): assert bootstrap_ci([0.7] * 20) == (0.7, 0.7, 0.7)
def test_bootstrap_brackets():
    m, lo, hi = bootstrap_ci([0.0, 1.0] * 50)
    assert m == 0.5 and lo < 0.5 < hi
def test_bootstrap_none():     assert bootstrap_ci([None, None]) is None
def test_pass_at_k():          assert pass_at_k(4, 2, 2) == pytest.approx(5 / 6)
def test_pass_hat_k():         assert pass_hat_k(4, 2, 2) == pytest.approx(1 / 6)
def test_pass_hat_k_edges():   assert pass_hat_k(4, 4, 4) == 1.0 and pass_hat_k(4, 3, 4) == 0.0
def test_mcnemar_extreme():    assert mcnemar_p([True] * 10, [False] * 10) == pytest.approx(2 * 0.5**10)
def test_mcnemar_identical():  assert mcnemar_p([True, False], [True, False]) == 1.0
```

- [ ] **Step 2:** Run → FAIL. **Step 3:** Implement. **Step 4:** Tests → PASS.
- [ ] **Step 5: Commit** `feat: add bootstrap CIs, pass@k/pass^k and McNemar`

---

### Task 9: Eval runner, CLI and first published numbers

**Files:**
- Create: `backend/src/sluice/evals/runner.py`, `backend/tests/evals/test_runner.py`, `evals/RESULTS.md`
- Modify: `backend/src/sluice/cli.py` (add `eval`, `results`), `Makefile` (add `eval-rules`, `results`)

**Interfaces:**
- Consumes: everything above.
- Produces:
  - `SYSTEMS: dict[str, Callable[[], System]] = {"rules": RulesOnlySystem}` (P2 registers agent systems here).
  - `EvalSummary(system: str, split: str, trials: int, n_cases: int, metrics: dict[str, tuple[float, float, float] | None], pass_hat_k: float | None, by_difficulty: dict[str, dict[str, tuple[float, float, float] | None]])` — `metrics` keys: `cell_accuracy, fabrication_rate, row_recall, row_precision, contract_pass, success, cost_usd, latency_s`; each is a bootstrap CI over per-case means across trials. `pass_hat_k` = mean over cases of `pass_hat_k(trials, successes, trials)`, `None` when `trials == 1`.
  - `run_eval(system: str, split: str, trials: int, cases_dir: Path, out_dir: Path) -> EvalSummary` — writes `out_dir/results.jsonl` (one line per case × trial: `case_id, trial, difficulty`, every `Grade` field, `cost_usd, latency_s`) and `out_dir/summary.json`.
  - CLI: `sluice eval --system rules --split dev --trials 1` → `evals/runs/<UTC timestamp>-<system>-<split>/`; `sluice results` renders `evals/RESULTS.md` from the latest summary per (system, split): main table *System · Split · n · Trials · Cell accuracy · Fabrication · Row recall · Contract pass · Success*, values formatted `81.4% [79.0, 83.6]` (`—` for `None`); a per-difficulty table; footer with the generating command, the `LOCK.json` hash for the split, and the date.

- [ ] **Step 1: Write failing test:**

```python
def test_run_eval_small(tmp_path):
    # build_split("dev", 6, 1, tmp_path / "cases"); s = run_eval("rules", "dev", 2, tmp_path / "cases", tmp_path / "out")
    # results.jsonl has 12 lines; lo <= mean <= hi for s.metrics["cell_accuracy"];
    # s.pass_hat_k is not None; set(s.by_difficulty) <= {"easy", "medium", "hard"}
```

- [ ] **Step 2:** Run → FAIL. **Step 3:** Implement. **Step 4:** Test → PASS.
- [ ] **Step 5:** Full run: `make cases && cd backend && uv run sluice eval --system rules --split dev --trials 1 && uv run sluice results`. Expected shape: high accuracy on *easy*, low on *medium/hard*, **fabrication rate 0.0** (rules never guess). If fabrication > 0, that is a Task 6 bug — fix it before committing numbers.
- [ ] **Step 6:** `make lint && make test` → pass.
- [ ] **Step 7: Commit** `feat: add eval runner and publish rules-only baseline` (commit `evals/RESULTS.md`; `evals/runs/` stays git-ignored).

---

## Done when

- `make lint` and `make test` pass locally and in GitHub Actions.
- `sluice cases --verify` exits 0 against the committed `LOCK.json`.
- `evals/RESULTS.md` shows the rules-only baseline on `dev` (n = 300) with 95% CIs, per difficulty, and fabrication rate 0.0.
