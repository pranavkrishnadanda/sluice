from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import polars as pl
import pytest

from sluice.contracts.model import ColumnSpec, Contract, load_contract
from sluice.contracts.validate import validate
from sluice.drift.cases import Recipe, build_case
from sluice.drift.writers import write_input
from sluice.evals.types import CaseInput, System, SystemOutput
from sluice.io import read_input
from sluice.rules.fixers import RuleResult, apply_rules
from sluice.rules.profile import Profile, profile
from sluice.systems.rules_only import RulesOnlySystem

N = 20


@pytest.fixture(scope="module")
def orders() -> Contract:
    return load_contract("orders")


@pytest.fixture(scope="module")
def lineitem() -> Contract:
    return load_contract("lineitem")


@pytest.fixture(scope="module")
def orders20(orders_df: pl.DataFrame) -> pl.DataFrame:
    return orders_df.head(N)


@pytest.fixture(scope="module")
def clean(orders_df: pl.DataFrame, lineitem_df: pl.DataFrame) -> dict[str, pl.DataFrame]:
    return {"orders": orders_df, "lineitem": lineitem_df}


@pytest.fixture(scope="module")
def li_reference(orders_df: pl.DataFrame) -> dict[str, set]:
    return {"orders.o_orderkey": set(orders_df["o_orderkey"].to_list())}


def _as_csv(df: pl.DataFrame, tmp_path: Path) -> pl.DataFrame:
    """Round-trip ``df`` through the case writer's CSV rendering (all-String frame)."""
    return read_input(write_input(df, tmp_path / "input", "csv", "utf-8"))


def _set(df: pl.DataFrame, col: str, values: list[object]) -> pl.DataFrame:
    """Copy of String frame ``df`` with the first ``len(values)`` cells of ``col`` replaced."""
    new = values + df[col].to_list()[len(values) :]
    return df.with_columns(pl.Series(col, new, dtype=pl.String))


def _run(df: pl.DataFrame, contract: Contract, reference: dict | None = None) -> RuleResult:
    return apply_rules(df, contract, profile(df, contract), reference or {})


def _pk(df: pl.DataFrame, i: int, contract: Contract) -> tuple:
    return tuple(df.row(i, named=True)[k] for k in contract.primary_key)


# --- the brief's tests ------------------------------------------------------------------------


def test_rules_fix_whitespace_and_case(orders: Contract, orders20, tmp_path: Path) -> None:
    df = _set(_as_csv(orders20, tmp_path), "o_orderstatus", [" o ", "f  ", " P"])
    out = _run(df, orders)
    assert out.df["o_orderstatus"].to_list()[:3] == ["O", "F", "P"]
    assert not out.flags


def test_rules_parse_dmy(orders: Contract, orders20, tmp_path: Path) -> None:
    df = _set(_as_csv(orders20, tmp_path), "o_orderdate", ["03/10/2026"])
    out = _run(df, orders)
    assert out.df["o_orderdate"][0] == date(2026, 10, 3)
    assert out.df["o_orderdate"].to_list()[1:] == orders20["o_orderdate"].to_list()[1:]


def test_rules_dont_guess_indian_numbers(orders: Contract, orders20, tmp_path: Path) -> None:
    df = _set(_as_csv(orders20, tmp_path), "o_totalprice", ["1,24,500.50"])
    out = _run(df, orders)
    assert out.df["o_totalprice"][0] is None
    assert ((_pk(orders20, 0, orders), "o_totalprice")) in out.flags
    assert out.df["o_totalprice"].to_list()[1:] == orders20["o_totalprice"].to_list()[1:]


def test_rules_flag_missing_column(orders: Contract, orders20, tmp_path: Path) -> None:
    df = _as_csv(orders20, tmp_path).drop("o_clerk")
    out = _run(df, orders)
    assert out.df["o_clerk"].null_count() == N
    pks = [_pk(orders20, i, orders) for i in range(N)]
    assert {(pk, "o_clerk") for pk in pks} == out.flags


def test_rules_dedupe(orders: Contract, orders20, tmp_path: Path) -> None:
    text = _as_csv(orders20, tmp_path)
    exact = text[:5]
    noisy = _set(text[5:8], "o_orderstatus", [f" {v.lower()} " for v in text["o_orderstatus"][5:8]])
    same_pk = _set(text[8:9], "o_clerk", ["Clerk#999999999"])  # same PK, other content
    df = pl.concat([text, exact, noisy, same_pk])
    out = _run(df, orders)
    assert out.df.height == N == out.df["o_orderkey"].n_unique()
    assert out.df.equals(orders20)  # first occurrence kept, in input order


def test_rules_fk_quarantine(lineitem: Contract, lineitem_df, li_reference, tmp_path) -> None:
    clean20 = lineitem_df.head(N)
    df = _set(_as_csv(clean20, tmp_path), "l_orderkey", ["-5"])
    out = _run(df, lineitem, li_reference)
    bad_pk = (-5, clean20["l_linenumber"][0])
    assert out.quarantined == {bad_pk}
    assert out.df.height == N - 1
    assert bad_pk not in set(out.df.select(lineitem.primary_key).rows())


def test_rules_easy_case_passes_contract(tmp_path: Path, clean) -> None:
    recipes = [
        Recipe("orders", "easy", [("whitespace", {"column": "o_orderstatus", "frac": 0.3})], *fmt)
        for fmt in (("csv", "utf-8", 60), ("parquet", "utf-8", 60))
    ] + [
        Recipe(
            "orders",
            "easy",
            [("case_noise", {"column": "o_orderpriority", "frac": 0.3})],
            "json_nested",
            "utf-8",
            60,
        ),
        Recipe("orders", "easy", [("duplicate_rows", {"frac": 0.2})], "csv", "utf-8-sig", 60),
        Recipe(
            "lineitem",
            "easy",
            [("date_format_mix", {"column": "l_shipdate", "frac": 0.3, "formats": ["%d/%m/%Y"]})],
            "json",
            "utf-8",
            60,
        ),
    ]
    system = RulesOnlySystem()
    for i, recipe in enumerate(recipes):
        case = build_case(f"dev-{i:04d}", "dev", recipe, clean, 5 + i, tmp_path)
        contract = load_contract(case.table)
        reference = {"orders.o_orderkey": set(clean["orders"]["o_orderkey"].to_list())}
        ref = reference if case.table == "lineitem" else {}
        result = system.run(CaseInput(case.input_path, contract, ref))
        report = validate(contract, result.df, ref, flags=result.flags)
        assert report.passed, (recipe, [v.message for v in report.violations])
        assert result.df.equals(case.expected()), recipe


# --- profiler ---------------------------------------------------------------------------------


def test_profile_matching_exact_case_insensitive_and_nested(orders: Contract) -> None:
    df = pl.DataFrame(
        {
            "o_orderkey": ["1"],
            " O-ORDERSTATUS ": ["O"],
            "details.o_clerk": ["c"],
            "orderdate": ["1996-01-02"],  # a rename: fuzzy, so never matched by rules
            "source_system": ["x"],
        }
    )
    prof = profile(df, orders)
    assert isinstance(prof, Profile)
    matched = {n: c.matched_contract_column for n, c in prof.columns.items()}
    assert matched == {
        "o_orderkey": "o_orderkey",
        " O-ORDERSTATUS ": "o_orderstatus",
        "details.o_clerk": "o_clerk",
        "orderdate": None,
        "source_system": None,
    }
    assert prof.extra == ["orderdate", "source_system"]
    assert "o_orderdate" in prof.missing and "o_orderkey" not in prof.missing
    assert prof.n_rows == 1


def test_profile_ambiguous_names_match_nothing_but_exact_wins(orders: Contract) -> None:
    df = pl.DataFrame({"O_CLERK": ["a"], "o-clerk": ["b"], "o_comment": ["c"], "O_COMMENT": ["d"]})
    matched = {n: c.matched_contract_column for n, c in profile(df, orders).columns.items()}
    expected = {"O_CLERK": None, "o-clerk": None, "o_comment": "o_comment", "O_COMMENT": None}
    assert matched == expected


def test_profile_stats(orders: Contract) -> None:
    df = pl.DataFrame({"o_clerk": ["a", None, "a", "b"]})
    col = profile(df, orders).columns["o_clerk"]
    assert (col.null_frac, col.distinct, col.sample) == (0.25, 2, ["a", "b"])


# --- fixers -----------------------------------------------------------------------------------


@pytest.mark.parametrize("token", ["N/A", "", "-1", "###"])
def test_rules_sentinel_tokens_null_and_flag(orders: Contract, orders20, tmp_path, token) -> None:
    df = _set(_as_csv(orders20, tmp_path), "o_custkey", [token])
    out = _run(df, orders)
    assert out.df["o_custkey"][0] is None
    assert out.flags == {(_pk(orders20, 0, orders), "o_custkey")}


def test_rules_int_sentinel_in_parquet(orders: Contract, orders20) -> None:
    df = orders20.with_columns(pl.Series("o_custkey", [-1] + orders20["o_custkey"].to_list()[1:]))
    out = _run(df, orders)
    assert out.df["o_custkey"][0] is None
    assert out.flags == {(_pk(orders20, 0, orders), "o_custkey")}


def test_rules_typed_parquet_passes_through_unchanged(orders: Contract, orders20) -> None:
    out = _run(orders20, orders)
    assert out.df.equals(orders20)
    assert not out.flags and not out.quarantined


def test_rules_parse_ist_timestamp(orders: Contract, orders20, tmp_path: Path) -> None:
    df = _set(_as_csv(orders20, tmp_path), "o_ordertime", ["1996-01-02 10:50:00+05:30"])
    out = _run(df, orders)
    assert out.df["o_ordertime"][0] == datetime(1996, 1, 2, 5, 20, tzinfo=UTC)
    assert out.df["o_ordertime"].to_list()[1:] == orders20["o_ordertime"].to_list()[1:]


def test_rules_naive_timestamp_not_guessed(orders: Contract, orders20, tmp_path: Path) -> None:
    df = _set(_as_csv(orders20, tmp_path), "o_ordertime", ["1996-01-02 10:50:00"])
    out = _run(df, orders)
    assert out.df["o_ordertime"][0] is None
    assert (_pk(orders20, 0, orders), "o_ordertime") in out.flags


@pytest.mark.parametrize("raw", ["02 Jan 1996", "1996/01/02", "13/13/2026", "19960102"])
def test_rules_unparsed_dates_flagged(orders: Contract, orders20, tmp_path, raw) -> None:
    df = _set(_as_csv(orders20, tmp_path), "o_orderdate", [raw])
    out = _run(df, orders)
    assert out.df["o_orderdate"][0] is None
    assert out.flags == {(_pk(orders20, 0, orders), "o_orderdate")}


def test_rules_dmy_only_for_day_first_locale(orders: Contract, orders20, tmp_path) -> None:
    us = Contract(orders.name, orders.version, orders.primary_key, orders.columns, locale="en_US")
    df = _set(_as_csv(orders20, tmp_path), "o_orderdate", ["03/10/2026"])
    assert _run(df, us).df["o_orderdate"][0] is None


@pytest.mark.parametrize("raw", ["12450050", "1245.005", "1e3", "12,450.50", " "])
def test_rules_money_needs_plain_decimal(orders: Contract, orders20, tmp_path, raw) -> None:
    # An integer string in a money (unit) column may be paise: never loaded as rupees.
    df = _set(_as_csv(orders20, tmp_path), "o_totalprice", [raw])
    out = _run(df, orders)
    assert out.df["o_totalprice"][0] is None
    assert out.flags == {(_pk(orders20, 0, orders), "o_totalprice")}


def test_rules_paise_int_column_in_parquet_not_loaded(orders: Contract, orders20) -> None:
    paise = orders20.with_columns((pl.col("o_totalprice") * 100).cast(pl.Int64))
    out = _run(paise, orders)
    assert out.df["o_totalprice"].null_count() == N
    assert len(out.flags) == N


def test_rules_unitless_decimal_accepts_integer_string(lineitem: Contract, lineitem_df, tmp_path):
    df = _set(_as_csv(lineitem_df.head(N), tmp_path), "l_quantity", ["17"])
    assert _run(df, lineitem).df["l_quantity"][0] == Decimal("17.00")


def test_rules_out_of_range_and_enum_flagged(orders: Contract, orders20, tmp_path) -> None:
    df = _set(_as_csv(orders20, tmp_path), "o_totalprice", ["-3.00", "600000.01"])
    df = _set(df, "o_orderstatus", ["X"])
    out = _run(df, orders)
    assert out.df["o_totalprice"].to_list()[:2] == [None, None]
    assert out.df["o_orderstatus"][0] is None
    pk0, pk1 = _pk(orders20, 0, orders), _pk(orders20, 1, orders)
    assert out.flags == {(pk0, "o_totalprice"), (pk1, "o_totalprice"), (pk0, "o_orderstatus")}


def test_rules_int32_overflow_flagged(orders: Contract, orders20, tmp_path: Path) -> None:
    df = _set(_as_csv(orders20, tmp_path), "o_shippriority", ["2147483648", "1,000"])
    out = _run(df, orders)
    assert out.df["o_shippriority"].to_list()[:2] == [None, None]
    assert len(out.flags) == 2


def test_rules_drop_extra_and_rename_case_insensitive(orders: Contract, orders20, tmp_path):
    df = _as_csv(orders20, tmp_path).rename({"o_clerk": "O_CLERK"})
    df = df.with_columns(pl.lit("x").alias("loaded_by"))
    out = _run(df, orders)
    assert out.df.columns == [c.name for c in orders.columns]
    assert out.df.schema == pl.Schema(orders.polars_schema())
    assert out.df["o_clerk"].to_list() == orders20["o_clerk"].to_list()


def test_rules_missing_pk_column_quarantines_file(orders: Contract, orders20, tmp_path: Path):
    df = _as_csv(orders20, tmp_path).rename({"o_orderkey": "orderkey"})
    system = RulesOnlySystem()
    path = tmp_path / "in.csv"
    df.write_csv(path)
    result = system.run(CaseInput(path, orders, {}))
    assert result.file_quarantined
    assert result.df.height == 0 and result.df.schema == pl.Schema(orders.polars_schema())
    assert not result.flags and not result.quarantined


def test_rules_free_text_whitespace_preserved(orders: Contract, orders20, tmp_path) -> None:
    # TPC-H comments carry real leading/trailing spaces; stripping them would corrupt truth.
    df = _set(_as_csv(orders20, tmp_path), "o_comment", ["  keep me "])
    assert _run(df, orders).df["o_comment"][0] == "  keep me "


def test_rules_quarantined_rows_carry_no_flags(lineitem: Contract, lineitem_df, li_reference):
    clean20 = lineitem_df.head(N)
    df = clean20.with_columns(
        pl.Series("l_orderkey", [-5] + clean20["l_orderkey"].to_list()[1:]),
        pl.Series("l_shipmode", ["N/A"] + clean20["l_shipmode"].to_list()[1:]),
    )
    out = _run(df, lineitem, li_reference)
    assert out.quarantined == {(-5, clean20["l_linenumber"][0])}
    assert not out.flags


def test_rules_flags_are_python_native_and_cells_null(orders: Contract, orders20, tmp_path):
    df = _set(_as_csv(orders20, tmp_path), "o_custkey", ["###", "N/A"]).drop("o_comment")
    out = _run(df, orders)
    by_pk = {_pk(out.df, i, orders): out.df.row(i, named=True) for i in range(out.df.height)}
    for pk, column in out.flags:
        assert all(type(v) in (int, str) for v in pk)
        assert by_pk[pk][column] is None


def test_rules_actions_recorded(orders: Contract, orders20, tmp_path: Path) -> None:
    df = _set(_as_csv(orders20, tmp_path), "o_custkey", ["N/A"]).drop("o_clerk")
    actions = _run(df, orders).actions
    assert any("o_clerk" in a and "missing" in a for a in actions)
    assert any("o_custkey" in a for a in actions)


def test_rules_only_system_protocol_and_output(orders: Contract, orders20, tmp_path) -> None:
    path = write_input(orders20, tmp_path / "input", "json", "utf-8")
    system = RulesOnlySystem()
    assert isinstance(system, System) and system.name == "rules"
    result = system.run(CaseInput(path, orders, {}))
    assert isinstance(result, SystemOutput)
    assert result.df.equals(orders20)
    assert (result.cost_usd, result.file_quarantined) == (0.0, False)
    assert result.latency_s >= 0.0
    assert "actions" in result.meta


def test_rules_deterministic(orders: Contract, orders20, tmp_path: Path) -> None:
    df = _set(_as_csv(orders20, tmp_path), "o_orderdate", ["bad", "03/10/2026"])
    a, b = _run(df, orders), _run(df, orders)
    assert a.df.equals(b.df) and a.flags == b.flags and a.actions == b.actions


@pytest.mark.parametrize("raw", ["١٢", "१२३"])
def test_rules_only_ascii_digits_parse(orders: Contract, orders20, tmp_path, raw) -> None:
    df = _set(_as_csv(orders20, tmp_path), "o_custkey", [raw])
    df = _set(df, "o_totalprice", [raw + ".00"])
    out = _run(df, orders)
    assert out.df["o_custkey"][0] is None and out.df["o_totalprice"][0] is None


def test_rules_dedupe_keeps_flag_of_bad_row_over_true_null(orders: Contract, orders20, tmp_path):
    # o_custkey nullable: "abc" repairs to null + flag, a true null to null without flag.
    cols = tuple(
        ColumnSpec(c.name, c.dtype, True) if c.name == "o_custkey" else c for c in orders.columns
    )
    nullable = Contract(orders.name, orders.version, orders.primary_key, cols)
    text = _as_csv(orders20, tmp_path)
    bad = _set(text[:1], "o_custkey", ["abc"])
    true_null = _set(text[:1], "o_custkey", [None])
    out = _run(pl.concat([bad, true_null, text[1:]]), nullable)
    assert out.df.height == N
    assert out.flags == {(_pk(orders20, 0, orders), "o_custkey")}


def test_profile_nested_leaf_ambiguous_matches_nothing(orders: Contract) -> None:
    df = pl.DataFrame({"o_orderkey": ["1"], "details.o_clerk": ["a"], "meta.O_CLERK": ["b"]})
    prof = profile(df, orders)
    assert prof.columns["details.o_clerk"].matched_contract_column is None
    assert prof.columns["meta.O_CLERK"].matched_contract_column is None
    assert "o_clerk" in prof.missing


def test_profile_nested_leaf_needs_exactly_one_container(orders: Contract) -> None:
    df = pl.DataFrame({"a.b.o_clerk": ["a"], ".o_comment": ["b"], "details.": ["c"]})
    matched = {n: c.matched_contract_column for n, c in profile(df, orders).columns.items()}
    assert matched == {"a.b.o_clerk": None, ".o_comment": None, "details.": None}


def test_rules_unkeyed_rows_counted_in_meta(orders: Contract, orders20, tmp_path) -> None:
    df = _set(_as_csv(orders20, tmp_path), "o_orderkey", ["abc", "1,000"])
    path = tmp_path / "in.csv"
    df.write_csv(path)
    result = RulesOnlySystem().run(CaseInput(path, orders, {}))
    assert result.meta["dropped_unkeyed_rows"] == 2
    assert result.df.height == N - 2
    assert any("primary key could not be parsed" in a for a in result.meta["actions"])


def test_rules_meta_dropped_unkeyed_rows_zero_by_default(orders: Contract, orders20, tmp_path):
    path = write_input(orders20, tmp_path / "input", "csv", "utf-8")
    assert RulesOnlySystem().run(CaseInput(path, orders, {})).meta["dropped_unkeyed_rows"] == 0


def test_rules_dedupe_prefers_flagged_row_even_when_second(orders: Contract, orders20, tmp_path):
    # Same PK: a genuinely null cell first, a null + flagged cell second -> the flag survives.
    cols = tuple(
        ColumnSpec(c.name, c.dtype, True) if c.name == "o_custkey" else c for c in orders.columns
    )
    nullable = Contract(orders.name, orders.version, orders.primary_key, cols)
    text = _as_csv(orders20, tmp_path)
    true_null = _set(text[:1], "o_custkey", [None])
    bad = _set(text[:1], "o_custkey", ["abc"])
    out = _run(pl.concat([true_null, bad, text[1:]]), nullable)
    assert out.flags == {(_pk(orders20, 0, orders), "o_custkey")}
    assert out.df["o_orderkey"].to_list() == orders20["o_orderkey"].to_list()  # order kept


def test_contract_sources_maps_contract_columns_to_input_columns() -> None:
    """Final review minor #4: the one source map used by the planner and the pipeline."""
    import polars as pl

    from sluice.contracts.model import load_contract
    from sluice.rules.profile import contract_sources, profile

    df = pl.DataFrame({" O_OrderKey ": ["1"], "junk": ["x"], "o_comment": ["c"]})
    assert contract_sources(profile(df, load_contract("orders"))) == {
        "o_orderkey": " O_OrderKey ",
        "o_comment": "o_comment",
    }


def test_flagged_raw_rows_lists_every_duplicate_copy() -> None:
    """N-1: per raw row, before dedupe, so both copies of a duplicate row are listed."""
    import polars as pl

    from sluice.contracts.model import load_contract
    from sluice.rules.fixers import flagged_raw_rows
    from sluice.rules.profile import profile

    contract = load_contract("orders")
    df = pl.DataFrame(
        {"o_orderkey": ["1", "2", "1"], "o_totalprice": ["1,234.50", "99.00", "1,234.50"]}
    )
    flagged = flagged_raw_rows(df, contract, profile(df, contract))
    assert flagged["o_totalprice"] == {0, 2}
    assert "o_custkey" not in flagged  # missing from the input: not a raw-row flag
