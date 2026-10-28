import json
from collections import Counter
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import duckdb
import numpy as np
import polars as pl
import pytest
from typer.testing import CliRunner

from sluice.cli import app
from sluice.contracts.model import load_contract
from sluice.drift.cases import (
    MANIFEST_KEYS,
    Case,
    Recipe,
    allocate,
    apply_recipe,
    build_case,
    build_split,
    load_case,
    load_reference,
    plan_split,
    sample_recipe,
    severe_options,
    split_hash,
    unexpected_files,
)
from sluice.drift.ops import DriftError
from sluice.drift.writers import write_input
from sluice.io import read_input

TABLES = ("orders", "lineitem")
DIFFICULTIES = ("easy", "medium", "hard")
RECIPES_PER_CELL = 200


@pytest.fixture(scope="session")
def clean(orders_df: pl.DataFrame, lineitem_df: pl.DataFrame) -> dict[str, pl.DataFrame]:
    return {"orders": orders_df, "lineitem": lineitem_df}


def _hard_lineitem_recipe(fmt: str = "csv", encoding: str = "utf-8") -> Recipe:
    return Recipe(
        table="lineitem",
        difficulty="hard",
        ops=[
            ("rename", {"column": "l_shipmode", "new_name": "shipmode"}),
            ("destroy", {"column": "l_quantity", "frac": 0.1, "token": "N/A"}),
            ("fk_break", {"column": "l_orderkey", "frac": 0.05}),
            ("duplicate_rows", {"frac": 0.05}),
        ],
        fmt=fmt,
        encoding=encoding,
        n_rows=500,
    )


def _mixed_frame() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "k": pl.Series([1, 2], dtype=pl.Int64),
            "price": pl.Series([Decimal("1245.00"), None], dtype=pl.Decimal(15, 2)),
            "d": pl.Series([date(1996, 1, 2), date(1997, 12, 31)], dtype=pl.Date),
            "ts": pl.Series(
                [datetime(1996, 1, 2, 5, 20, tzinfo=UTC), datetime(1997, 1, 1, tzinfo=UTC)],
                dtype=pl.Datetime("us", "UTC"),
            ),
            "s": pl.Series(["café", " pad "], dtype=pl.String),
        }
    )


# --- writers ---------------------------------------------------------------------------------


def test_write_input_csv_serialises_decimal_date_datetime_plainly(tmp_path: Path) -> None:
    path = write_input(_mixed_frame(), tmp_path / "input", "csv", "utf-8")
    assert path == tmp_path / "input.csv"
    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "k,price,d,ts,s"
    assert lines[1] == "1,1245.00,1996-01-02,1996-01-02T05:20:00+00:00,café"
    assert lines[2] == "2,,1997-12-31,1997-01-01T00:00:00+00:00, pad "


def test_write_input_csv_utf8_sig_writes_bom(tmp_path: Path) -> None:
    path = write_input(_mixed_frame(), tmp_path / "input", "csv", "utf-8-sig")
    assert path.read_bytes().startswith(b"\xef\xbb\xbf")
    assert read_input(path).columns == ["k", "price", "d", "ts", "s"]


def test_write_input_json_records_with_string_decimals(tmp_path: Path) -> None:
    path = write_input(_mixed_frame(), tmp_path / "input", "json", "utf-8")
    assert path.suffix == ".json"
    rows = json.loads(path.read_text(encoding="utf-8"))
    assert rows[0] == {
        "k": 1,
        "price": "1245.00",
        "d": "1996-01-02",
        "ts": "1996-01-02T05:20:00+00:00",
        "s": "café",
    }
    assert rows[1]["price"] is None


def test_write_input_json_nested_keeps_pk_top_level(tmp_path: Path) -> None:
    path = write_input(_mixed_frame(), tmp_path / "input", "json_nested", "utf-8", pk_columns=["k"])
    rows = json.loads(path.read_text(encoding="utf-8"))
    assert list(rows[0]) == ["k", "details"]
    assert rows[0]["details"]["price"] == "1245.00"
    assert read_input(path).columns == [
        "k",
        "details.price",
        "details.d",
        "details.ts",
        "details.s",
    ]


def test_write_input_json_nested_requires_pk_columns(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="pk_columns"):
        write_input(_mixed_frame(), tmp_path / "input", "json_nested", "utf-8")


def test_write_input_parquet_keeps_dtypes(tmp_path: Path) -> None:
    df = _mixed_frame()
    path = write_input(df, tmp_path / "input", "parquet", "latin-1")
    assert pl.read_parquet(path).equals(df)


def test_write_input_is_byte_identical(tmp_path: Path) -> None:
    for fmt in ("csv", "json", "json_nested", "parquet"):
        a = write_input(_mixed_frame(), tmp_path / f"a_{fmt}", fmt, "utf-8", pk_columns=["k"])
        b = write_input(_mixed_frame(), tmp_path / f"b_{fmt}", fmt, "utf-8", pk_columns=["k"])
        assert a.read_bytes() == b.read_bytes(), fmt


def test_latin1_written_and_readable(tmp_path: Path) -> None:
    path = write_input(_mixed_frame(), tmp_path / "input", "csv", "latin-1")
    raw = path.read_bytes()
    assert "café".encode("latin-1") in raw
    with pytest.raises(UnicodeDecodeError):
        raw.decode("utf-8")
    assert read_input(path)["s"].to_list() == ["café", " pad "]


# --- allocation and recipes ------------------------------------------------------------------


def test_allocate_largest_remainder_sums_to_n() -> None:
    for n in range(0, 40):
        counts = allocate(n, (30, 40, 30))
        assert sum(counts) == n
        assert all(abs(c - n * w / 100) < 1 for c, w in zip(counts, (30, 40, 30), strict=True))
    assert allocate(5, (60, 40)) == [3, 2]


def test_difficulty_mix() -> None:
    plan = plan_split("dev", 100, 1)
    assert Counter(p.recipe.difficulty for p in plan) == {"easy": 30, "medium": 40, "hard": 30}
    assert Counter(p.recipe.table for p in plan) == {"orders": 60, "lineitem": 40}
    assert [p.case_id for p in plan[:2]] == ["dev-0000", "dev-0001"]


def _assert_recipe_rules(recipe: Recipe) -> None:
    names = [op for op, _ in recipe.ops]
    params = dict(recipe.ops)
    if recipe.difficulty == "easy":
        assert len(names) == 1
        assert names[0] in {"whitespace", "case_noise", "duplicate_rows", "date_format_mix"}
        if names[0] == "date_format_mix":
            assert params["date_format_mix"]["formats"] == ["%d/%m/%Y"]
    elif recipe.difficulty == "medium":
        assert 2 <= len(names) <= 3
        assert {"rename", "indian_number_string", "tz_ist", "unit_paise"} & set(names)
        assert all(p["hinted"] for op, p in recipe.ops if op == "unit_paise")
        assert not {"destroy", "drop_column", "fk_break"} & set(names)
    else:
        assert 3 <= len(names) <= 5
        assert "rename" in names
        severe = {op for op, p in recipe.ops if op in {"destroy", "drop_column", "fk_break"}}
        severe |= {op for op, p in recipe.ops if op == "unit_paise" and not p["hinted"]}
        assert severe
    columns = [p["column"] for _, p in recipe.ops if "column" in p]
    assert len(columns) == len(set(columns)), "each column op must own its column"
    assert recipe.fmt in {"csv", "json", "json_nested", "parquet"}
    if recipe.encoding == "latin-1":
        assert recipe.fmt == "csv"
        assert recipe.difficulty != "easy"
        assert ("unicode_text", "o_comment" if recipe.table == "orders" else "l_comment") in [
            (op, p.get("column")) for op, p in recipe.ops
        ]
    assert recipe.n_rows == 500
    contract = load_contract(recipe.table)
    for op, p in recipe.ops:
        if op in {"destroy", "drop_column"}:
            assert p["column"] not in contract.primary_key
        if op == "unicode_text":
            assert p["column"] in {"o_comment", "l_comment"}
        if op == "case_noise":
            assert contract.column(p["column"]).enum is not None
        if op == "fk_break":
            assert recipe.table == "lineitem"


@pytest.mark.parametrize("table", TABLES)
@pytest.mark.parametrize("difficulty", DIFFICULTIES)
def test_sample_recipe_never_raises_drift_error(
    table: str, difficulty: str, clean: dict[str, pl.DataFrame]
) -> None:
    rng = np.random.default_rng(1234)
    for i in range(RECIPES_PER_CELL):
        recipe = sample_recipe(table, difficulty, rng)
        _assert_recipe_rules(recipe)
        try:
            apply_recipe(recipe, clean, seed=i)
        except DriftError as exc:  # pragma: no cover - failure path
            pytest.fail(f"recipe {i} raised {exc!r}: {recipe}")


def test_sample_recipe_covers_every_format_and_latin1() -> None:
    rng = np.random.default_rng(0)
    recipes = [sample_recipe("orders", "medium", rng) for _ in range(400)]
    assert {r.fmt for r in recipes} == {"csv", "json", "json_nested", "parquet"}
    csv = [r for r in recipes if r.fmt == "csv"]
    latin = sum(r.encoding == "latin-1" for r in csv)
    assert 0.1 < latin / len(csv) < 0.3


def test_sample_recipe_rejects_unknown_inputs() -> None:
    rng = np.random.default_rng(0)
    with pytest.raises(ValueError, match="difficulty"):
        sample_recipe("orders", "extreme", rng)
    with pytest.raises(ValueError, match="table"):
        sample_recipe("customer", "easy", rng)


# --- cases -----------------------------------------------------------------------------------


def test_build_case_files(tmp_path: Path, clean: dict[str, pl.DataFrame]) -> None:
    case = build_case("dev-0000", "dev", _hard_lineitem_recipe(), clean, 11, tmp_path)
    assert case.dir == tmp_path / "dev-0000"
    assert sorted(p.name for p in case.dir.iterdir()) == [
        "expected.parquet",
        "input.csv",
        "manifest.json",
    ]
    manifest = json.loads((case.dir / "manifest.json").read_text(encoding="utf-8"))
    assert tuple(manifest) == MANIFEST_KEYS
    assert MANIFEST_KEYS == (
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
    assert manifest["file_quarantine"] is False
    assert manifest["contract_version"] == "v1"
    assert [e["op"] for e in manifest["ops"]] == [op for op, _ in _hard_lineitem_recipe().ops]
    assert set(manifest["ops"][0]) == {"op", "params", "columns", "rows_affected"}
    assert case.input_path == case.dir / "input.csv"
    assert case.difficulty == "hard"


def test_build_case_lineitem_samples_whole_orders(
    tmp_path: Path, clean: dict[str, pl.DataFrame]
) -> None:
    recipe = Recipe(
        "lineitem", "easy", [("duplicate_rows", {"frac": 0.0})], "parquet", "utf-8", 500
    )
    case = build_case("dev-0001", "dev", recipe, clean, 3, tmp_path)
    expected = case.expected()
    assert case.manifest["n_rows"] == expected.height >= 500
    full = clean["lineitem"].group_by("l_orderkey").len()
    mine = expected.group_by("l_orderkey").len()
    assert (
        mine.join(full, on="l_orderkey", suffix="_full")
        .filter(pl.col("len") != pl.col("len_full"))
        .is_empty()
    )


def test_build_case_orders_has_n_rows(tmp_path: Path, clean: dict[str, pl.DataFrame]) -> None:
    recipe = Recipe("orders", "easy", [("duplicate_rows", {"frac": 0.1})], "json", "utf-8", 500)
    case = build_case("dev-0002", "dev", recipe, clean, 3, tmp_path)
    assert case.manifest["n_rows"] == 500
    assert case.expected().height == 500
    assert read_input(case.input_path).height == 550


def test_case_roundtrip(tmp_path: Path, clean: dict[str, pl.DataFrame]) -> None:
    recipe = _hard_lineitem_recipe(fmt="json_nested")
    state = apply_recipe(recipe, clean, seed=11)
    built = build_case("dev-0003", "dev", recipe, clean, 11, tmp_path)
    case = load_case(built.dir)
    assert isinstance(case, Case)
    assert state.flags and state.quarantined
    assert case.flags() == state.flags
    assert case.quarantined() == state.quarantined
    assert case.expected().equals(state.expected)
    assert case.manifest == built.manifest
    assert (case.case_id, case.split, case.table) == ("dev-0003", "dev", "lineitem")


def test_build_case_is_byte_identical(tmp_path: Path, clean: dict[str, pl.DataFrame]) -> None:
    a = build_case("dev-0004", "dev", _hard_lineitem_recipe(), clean, 5, tmp_path / "a")
    b = build_case("dev-0004", "dev", _hard_lineitem_recipe(), clean, 5, tmp_path / "b")
    assert split_hash([a]) == split_hash([b])


def test_split_deterministic(tmp_path: Path) -> None:
    a = build_split("dev", 5, 1, tmp_path / "a")
    b = build_split("dev", 5, 1, tmp_path / "b")
    assert [c.case_id for c in a] == [f"dev-{i:04d}" for i in range(5)]
    assert split_hash(a) == split_hash(b)
    assert split_hash(a) != split_hash(build_split("dev", 5, 2, tmp_path / "c"))


def test_build_split_replaces_stale_cases(tmp_path: Path) -> None:
    build_split("dev", 3, 1, tmp_path)
    build_split("dev", 2, 1, tmp_path)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["dev-0000", "dev-0001"]


def test_dev_holdout_disjoint(tmp_path: Path) -> None:
    dev = build_split("dev", 5, 1, tmp_path / "dev")
    holdout = build_split("holdout", 5, 1, tmp_path / "holdout")
    dev_bytes = {(c.dir / "expected.parquet").read_bytes() for c in dev}
    holdout_bytes = {(c.dir / "expected.parquet").read_bytes() for c in holdout}
    assert not dev_bytes & holdout_bytes
    assert all(c.split == "holdout" and c.case_id.startswith("holdout-") for c in holdout)


def test_split_hash_detects_tampering(tmp_path: Path) -> None:
    cases = build_split("dev", 2, 1, tmp_path)
    before = split_hash(cases)
    manifest = cases[0].dir / "manifest.json"
    manifest.write_bytes(manifest.read_bytes() + b" ")
    assert split_hash(cases) != before


def test_load_reference() -> None:
    assert load_reference("orders") == {}
    ref = load_reference("lineitem")
    assert set(ref) == {"orders.o_orderkey"}
    assert len(ref["orders.o_orderkey"]) == 15000
    assert all(isinstance(k, int) for k in list(ref["orders.o_orderkey"])[:10])


# --- CLI -------------------------------------------------------------------------------------


def test_cli_cases_build_lock_verify(tmp_path: Path) -> None:
    runner = CliRunner()
    base = ["cases", "--cases-dir", str(tmp_path)]
    for split in ("dev", "holdout"):
        result = runner.invoke(app, [*base, "--split", split, "--n", "2"])
        assert result.exit_code == 0, result.output
    assert runner.invoke(app, [*base, "--lock"]).exit_code == 0
    lock = json.loads((tmp_path / "LOCK.json").read_text(encoding="utf-8"))
    assert set(lock) == {"environment", "splits"}
    assert lock["environment"] == {"duckdb": duckdb.__version__, "polars": pl.__version__}
    assert set(lock["splits"]) == {"dev", "holdout"}
    assert lock["splits"]["dev"]["n_cases"] == 2
    assert runner.invoke(app, [*base, "--verify"]).exit_code == 0

    manifest = tmp_path / "dev" / "dev-0000" / "manifest.json"
    manifest.write_bytes(manifest.read_bytes() + b"\n")
    result = runner.invoke(app, [*base, "--verify"])
    assert result.exit_code == 1
    assert "dev" in result.output


def test_cli_cases_verify_without_lock_fails(tmp_path: Path) -> None:
    result = CliRunner().invoke(app, ["cases", "--cases-dir", str(tmp_path), "--verify"])
    assert result.exit_code == 1


def test_cli_cases_requires_an_action(tmp_path: Path) -> None:
    result = CliRunner().invoke(app, ["cases", "--cases-dir", str(tmp_path)])
    assert result.exit_code != 0


def test_split_hash_ignores_dotfiles(tmp_path: Path) -> None:
    cases = build_split("dev", 1, 1, tmp_path)
    before = split_hash(cases)
    (cases[0].dir / ".DS_Store").write_bytes(b"finder")
    assert split_hash(cases) == before


# --- fix round 1: latin-1, severe stratification, version stamp, stray files ----------------


def _has_high_byte(path: Path) -> bool:
    return any(b > 127 for b in path.read_bytes())


def test_latin1_cases_contain_non_ascii(tmp_path: Path, clean: dict[str, pl.DataFrame]) -> None:
    rng = np.random.default_rng(99)
    latin = []
    for i in range(600):
        recipe = sample_recipe(TABLES[i % 2], DIFFICULTIES[i % 3], rng)
        if recipe.encoding == "latin-1":
            latin.append(recipe)
    assert len(latin) >= 10
    for i, recipe in enumerate(latin):
        case = build_case(f"dev-{i:04d}", "dev", recipe, clean, i, tmp_path)
        assert _has_high_byte(case.input_path), recipe


def test_easy_csv_is_never_latin1() -> None:
    rng = np.random.default_rng(5)
    recipes = [sample_recipe("orders", "easy", rng) for _ in range(400)]
    assert all(r.encoding != "latin-1" for r in recipes)
    assert any(r.fmt == "csv" for r in recipes)


def test_hard_severe_op_is_stratified() -> None:
    plan = plan_split("holdout", 300, 1)
    hard = [p for p in plan if p.recipe.difficulty == "hard"]
    by_table = {t: Counter(p.severe for p in hard if p.recipe.table == t) for t in TABLES}
    assert by_table["orders"] == {"unit_paise": 18, "destroy": 18, "drop_column": 18}
    assert by_table["lineitem"] == {
        "unit_paise": 9,
        "destroy": 9,
        "drop_column": 9,
        "fk_break": 9,
    }
    for p in hard:
        params = [prm for op, prm in p.recipe.ops if op == p.severe]
        assert params, p
        if p.severe == "unit_paise":
            assert params[0]["hinted"] is False
    assert all(p.severe is None for p in plan if p.recipe.difficulty != "hard")


def test_table_mix_is_stratified_within_difficulty() -> None:
    plan = plan_split("dev", 300, 1)
    cells = Counter((p.recipe.difficulty, p.recipe.table) for p in plan)
    assert cells == {
        ("easy", "orders"): 54,
        ("easy", "lineitem"): 36,
        ("medium", "orders"): 72,
        ("medium", "lineitem"): 48,
        ("hard", "orders"): 54,
        ("hard", "lineitem"): 36,
    }


def test_severe_options_and_validation() -> None:
    assert severe_options("orders") == ("unit_paise", "destroy", "drop_column")
    assert severe_options("lineitem") == ("unit_paise", "destroy", "drop_column", "fk_break")
    rng = np.random.default_rng(0)
    with pytest.raises(ValueError, match="severe"):
        sample_recipe("orders", "hard", rng, severe="fk_break")
    with pytest.raises(ValueError, match="severe"):
        sample_recipe("lineitem", "medium", rng, severe="destroy")


def test_build_case_clears_existing_case_dir(
    tmp_path: Path, clean: dict[str, pl.DataFrame]
) -> None:
    (tmp_path / "dev-0000").mkdir()
    (tmp_path / "dev-0000" / "manifest 2.json").write_text("{}", encoding="utf-8")
    case = build_case("dev-0000", "dev", _hard_lineitem_recipe(), clean, 1, tmp_path)
    assert sorted(p.name for p in case.dir.iterdir()) == [
        "expected.parquet",
        "input.csv",
        "manifest.json",
    ]


def test_build_split_removes_conflict_copies(tmp_path: Path) -> None:
    build_split("dev", 2, 1, tmp_path)
    (tmp_path / "dev-0000 2").mkdir()
    (tmp_path / "dev-0001" / "expected 2.parquet").write_bytes(b"x")
    build_split("dev", 2, 1, tmp_path)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["dev-0000", "dev-0001"]
    assert unexpected_files(tmp_path) == []


def test_unexpected_files_lists_strays(tmp_path: Path) -> None:
    cases = build_split("dev", 2, 1, tmp_path)
    assert unexpected_files(tmp_path) == []
    (cases[0].dir / ".DS_Store").write_bytes(b"finder")
    conflict = cases[0].dir / "manifest 2.json"
    conflict.write_text("{}", encoding="utf-8")
    other_input = cases[1].dir / (
        "input.parquet" if cases[1].manifest["format"] != "parquet" else "input.csv"
    )
    other_input.write_bytes(b"x")
    stray_dir = tmp_path / "dev-0000 2"
    stray_dir.mkdir()
    assert unexpected_files(tmp_path) == sorted([conflict, other_input, stray_dir])


def _locked(tmp_path: Path) -> list[str]:
    runner = CliRunner()
    base = ["cases", "--cases-dir", str(tmp_path)]
    assert runner.invoke(app, [*base, "--split", "dev", "--n", "2", "--lock"]).exit_code == 0
    assert runner.invoke(app, [*base, "--verify"]).exit_code == 0
    return base


def test_cli_verify_fails_on_unexpected_files(tmp_path: Path) -> None:
    base = _locked(tmp_path)
    stray = tmp_path / "dev" / "dev-0001" / "input 2.csv"
    stray.write_bytes(b"x")
    result = CliRunner().invoke(app, [*base, "--verify"])
    assert result.exit_code == 1
    assert "unexpected" in result.output
    assert str(stray) in result.output


def test_cli_verify_reports_environment_change(tmp_path: Path) -> None:
    base = _locked(tmp_path)
    lock_path = tmp_path / "LOCK.json"
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    lock["environment"]["polars"] = "0.0.1"
    lock_path.write_text(json.dumps(lock), encoding="utf-8")
    result = CliRunner().invoke(app, [*base, "--verify"])
    assert result.exit_code == 1
    assert f"environment changed (polars 0.0.1 -> {pl.__version__})" in result.output
    assert "make cases" in result.output


@pytest.mark.parametrize("table", TABLES)
def test_forced_severe_recipes_never_raise(table: str, clean: dict[str, pl.DataFrame]) -> None:
    rng = np.random.default_rng(77)
    for severe in severe_options(table):
        for i in range(50):
            recipe = sample_recipe(table, "hard", rng, severe=severe)
            _assert_recipe_rules(recipe)
            assert severe in [op for op, _ in recipe.ops]
            apply_recipe(recipe, clean, seed=i)
