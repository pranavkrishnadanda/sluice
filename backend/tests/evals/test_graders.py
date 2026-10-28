"""Ground-truth graders, on hand-built 3-row ``orders`` cases (plus one real built case)."""

import json
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import polars as pl
import pytest

from sluice.contracts.model import Contract, load_contract
from sluice.drift.cases import Case, Recipe, build_case
from sluice.evals.graders import Grade, grade
from sluice.evals.types import PK, CaseInput, SystemOutput
from sluice.systems.rules_only import RulesOnlySystem

N_COLS = 10  # orders contract columns


@pytest.fixture(scope="module")
def orders() -> Contract:
    return load_contract("orders")


def _truth(orders: Contract) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "o_orderkey": [1, 2, 3],
            "o_custkey": [10, 20, 30],
            "o_orderstatus": ["F", "O", "P"],
            "o_totalprice": [Decimal("100.10"), Decimal("200.20"), Decimal("300.30")],
            "o_orderdate": [date(1995, 1, 1), date(1996, 2, 2), date(1997, 3, 3)],
            "o_ordertime": [
                datetime(1995, 1, 1, 10, 0, 0, tzinfo=UTC),
                datetime(1996, 2, 2, 11, 30, 0, tzinfo=UTC),
                datetime(1997, 3, 3, 12, 45, 15, tzinfo=UTC),
            ],
            "o_orderpriority": ["1-URGENT", "2-HIGH", "5-LOW"],
            "o_clerk": ["Clerk#1", "Clerk#2", "Clerk#3"],
            "o_shippriority": [0, 0, 0],
            "o_comment": ["a", "b", "c"],
        },
        schema=orders.polars_schema(),
    )


def _case(
    tmp_path: Path,
    expected: pl.DataFrame,
    flags: set[tuple[PK, str]] | None = None,
    quarantined: set[PK] | None = None,
) -> Case:
    case_dir = tmp_path / "dev-0000"
    case_dir.mkdir(parents=True)
    expected.write_parquet(case_dir / "expected.parquet")
    manifest: dict[str, Any] = {
        "case_id": "dev-0000",
        "split": "dev",
        "table": "orders",
        "contract_version": "v1",
        "difficulty": "hard",
        "format": "parquet",
        "expected_flags": [[list(pk), c] for pk, c in sorted(flags or set())],
        "expected_quarantined": [list(pk) for pk in sorted(quarantined or set())],
        "file_quarantine": False,
    }
    (case_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return Case("dev-0000", "dev", "orders", case_dir, manifest)


def _out(
    df: pl.DataFrame,
    flags: set[tuple[PK, str]] | None = None,
    quarantined: set[PK] | None = None,
    *,
    file_quarantined: bool = False,
) -> SystemOutput:
    return SystemOutput(
        df=df,
        flags=flags or set(),
        quarantined=quarantined or set(),
        file_quarantined=file_quarantined,
        cost_usd=0.0,
        latency_s=0.0,
    )


def _destroyed(orders: Contract) -> pl.DataFrame:
    """Truth with o_custkey of order 2 destroyed (null in truth, expected FLAG)."""
    t = _truth(orders)
    return t.with_columns(
        pl.when(pl.col("o_orderkey") == 2)
        .then(None)
        .otherwise(pl.col("o_custkey"))
        .alias("o_custkey")
    )


FLAG_2 = {((2,), "o_custkey")}


# --- the brief's eight -----------------------------------------------------------------------


def test_perfect_output(tmp_path: Path, orders: Contract) -> None:
    t = _truth(orders)
    g = grade(_case(tmp_path, t), _out(t), {})
    assert isinstance(g, Grade)
    assert g.cell_accuracy == 1.0
    assert g.fabrication_rate is None
    assert g.success is True
    assert (g.row_recall, g.row_precision, g.fabricated_rows) == (1.0, 1.0, 0)
    assert (g.n_cells, g.n_flag_cells, g.extra_columns) == (3 * N_COLS, 0, [])
    assert (g.case_id, g.difficulty, g.contract_pass) == ("dev-0000", "hard", True)


def test_flag_correct(tmp_path: Path, orders: Contract) -> None:
    truth = _destroyed(orders)
    g = grade(_case(tmp_path, truth, FLAG_2), _out(truth, FLAG_2), {})
    assert g.cell_accuracy == 1.0
    assert g.fabrication_rate == 0.0
    assert g.n_flag_cells == 1
    assert g.contract_pass is True  # flagged null is exempt from not_null (Ruling A)
    assert g.success is True


def test_fabrication(tmp_path: Path, orders: Contract) -> None:
    g = grade(_case(tmp_path, _destroyed(orders), FLAG_2), _out(_truth(orders)), {})
    assert g.fabrication_rate == 1.0
    assert g.cell_accuracy is not None and g.cell_accuracy < 1.0
    assert g.cell_accuracy == pytest.approx((3 * N_COLS - 1) / (3 * N_COLS))
    assert g.success is False


def test_flag_when_recoverable(tmp_path: Path, orders: Contract) -> None:
    truth = _destroyed(orders)
    out_df = truth.with_columns(
        pl.when(pl.col("o_orderkey") == 3).then(None).otherwise(pl.col("o_clerk")).alias("o_clerk")
    )
    flags = FLAG_2 | {((3,), "o_clerk")}
    g = grade(_case(tmp_path, truth, FLAG_2), _out(out_df, flags), {})
    assert g.cell_accuracy == pytest.approx((3 * N_COLS - 1) / (3 * N_COLS))
    assert g.fabrication_rate == 0.0
    assert g.success is False


def test_missing_row(tmp_path: Path, orders: Contract) -> None:
    t = _truth(orders)
    g = grade(_case(tmp_path, t), _out(t.filter(pl.col("o_orderkey") != 2)), {})
    assert g.row_recall == pytest.approx(2 / 3)
    assert g.row_precision == 1.0
    assert g.cell_accuracy == pytest.approx(2 / 3)  # all 10 cells of the absent row wrong
    assert g.fabricated_rows == 0
    assert g.success is False


def test_zero_output_rows(tmp_path: Path, orders: Contract) -> None:
    t = _truth(orders)
    g = grade(_case(tmp_path, t), _out(t.clear()), {})
    assert g.row_precision is None
    assert g.row_recall == 0.0
    assert g.cell_accuracy == 0.0
    assert g.success is False


def test_zero_expected_rows(tmp_path: Path, orders: Contract) -> None:
    t = _truth(orders)
    g = grade(_case(tmp_path, t.clear()), _out(t.clear()), {})
    assert g.cell_accuracy is None
    assert g.row_recall is None
    assert g.row_precision is None
    assert g.n_cells == 0
    assert g.success is False  # cell_accuracy None is not 1.0


def test_invented_row(tmp_path: Path, orders: Contract) -> None:
    t = _truth(orders)
    invented = t.head(1).with_columns(pl.lit(99, dtype=pl.Int64).alias("o_orderkey"))
    g = grade(_case(tmp_path, t), _out(pl.concat([t, invented])), {})
    assert g.fabricated_rows == 1
    assert g.cell_accuracy == 1.0
    assert g.row_precision == pytest.approx(3 / 4)
    assert g.success is False


# --- further edge cases ----------------------------------------------------------------------


def test_file_quarantined_means_no_output_rows(tmp_path: Path, orders: Contract) -> None:
    t = _truth(orders)
    g = grade(_case(tmp_path, t), _out(t, file_quarantined=True), {})
    assert (g.row_recall, g.row_precision, g.cell_accuracy) == (0.0, None, 0.0)
    assert g.fabricated_rows == 0


def test_quarantined_rows_are_not_fabricated(tmp_path: Path, orders: Contract) -> None:
    t = _truth(orders)
    kept = t.filter(pl.col("o_orderkey") != 3)
    leaked = t.filter(pl.col("o_orderkey") == 3).with_columns(
        pl.lit(-7, dtype=pl.Int64).alias("o_orderkey")
    )
    case = _case(tmp_path, kept, quarantined={(-7,)})
    g = grade(case, _out(pl.concat([kept, leaked])), {})
    assert g.fabricated_rows == 0
    assert g.row_precision == pytest.approx(2 / 3)
    assert g.cell_accuracy == 1.0


def test_flag_without_null_is_the_value(tmp_path: Path, orders: Contract) -> None:
    truth = _destroyed(orders)
    # Output flags the cell but still writes a value: that value is the prediction.
    g = grade(_case(tmp_path, truth, FLAG_2), _out(_truth(orders), FLAG_2), {})
    assert g.fabrication_rate == 1.0


def test_unflagged_null_where_truth_is_flag_is_wrong(tmp_path: Path, orders: Contract) -> None:
    truth = _destroyed(orders)
    g = grade(_case(tmp_path, truth, FLAG_2), _out(truth), {})
    assert g.cell_accuracy == pytest.approx((3 * N_COLS - 1) / (3 * N_COLS))
    assert g.fabrication_rate == 0.0  # null is not a fabricated value
    assert g.contract_pass is False  # unflagged null in a non-nullable column


def test_wrong_value_is_incorrect(tmp_path: Path, orders: Contract) -> None:
    t = _truth(orders)
    out_df = t.with_columns(
        pl.when(pl.col("o_orderkey") == 1)
        .then(pl.lit(Decimal("100.11")))
        .otherwise(pl.col("o_totalprice"))
        .cast(pl.Decimal(15, 2))
        .alias("o_totalprice")
    )
    g = grade(_case(tmp_path, t), _out(out_df), {})
    assert g.cell_accuracy == pytest.approx((3 * N_COLS - 1) / (3 * N_COLS))


def test_timestamps_compare_at_second_precision(tmp_path: Path, orders: Contract) -> None:
    t = _truth(orders)
    out_df = t.with_columns(pl.col("o_ordertime") + pl.duration(milliseconds=400))
    g = grade(_case(tmp_path, t), _out(out_df), {})
    assert g.cell_accuracy == 1.0
    off = t.with_columns(pl.col("o_ordertime") + pl.duration(seconds=1))
    g2 = grade(_case(tmp_path / "b", t), _out(off), {})
    assert g2.cell_accuracy == pytest.approx((3 * N_COLS - 3) / (3 * N_COLS))


def test_output_cast_to_contract_dtypes(tmp_path: Path, orders: Contract) -> None:
    t = _truth(orders)
    # Strings that cast exactly to the contract dtypes are compared after the cast.
    out_df = t.with_columns(
        pl.col("o_totalprice").cast(pl.String), pl.col("o_shippriority").cast(pl.Int64)
    )
    g = grade(_case(tmp_path, t), _out(out_df), {})
    assert g.cell_accuracy == 1.0
    assert g.contract_pass is False  # dtype violations still fail the contract


def test_uncastable_column_counts_incorrect_and_fabricated(
    tmp_path: Path, orders: Contract
) -> None:
    truth = _destroyed(orders)
    out_df = truth.with_columns(pl.Series("o_custkey", ["x", "y", "z"], dtype=pl.String))
    g = grade(_case(tmp_path, truth, FLAG_2), _out(out_df), {})
    assert g.cell_accuracy == pytest.approx((3 * N_COLS - 3) / (3 * N_COLS))
    assert g.fabrication_rate == 1.0


def test_unsupported_dtype_never_crashes(tmp_path: Path, orders: Contract) -> None:
    t = _truth(orders)
    out_df = t.with_columns(pl.Series("o_orderdate", [[1], [2], [3]]))
    g = grade(_case(tmp_path, t), _out(out_df), {})
    assert g.cell_accuracy == pytest.approx((3 * N_COLS - 3) / (3 * N_COLS))


def test_missing_and_extra_columns(tmp_path: Path, orders: Contract) -> None:
    t = _truth(orders)
    out_df = t.drop("o_comment").with_columns(pl.lit("x").alias("loaded_by"))
    g = grade(_case(tmp_path, t), _out(out_df), {})
    assert g.extra_columns == ["loaded_by"]
    assert g.cell_accuracy == pytest.approx((3 * N_COLS - 3) / (3 * N_COLS))
    assert g.contract_pass is False


def test_missing_pk_column_means_no_addressable_rows(tmp_path: Path, orders: Contract) -> None:
    t = _truth(orders)
    g = grade(_case(tmp_path, t), _out(t.drop("o_orderkey")), {})
    assert (g.row_recall, g.row_precision, g.cell_accuracy, g.fabricated_rows) == (
        0.0,
        None,
        0.0,
        0,
    )


def test_duplicate_output_pks_are_scored_once(tmp_path: Path, orders: Contract) -> None:
    t = _truth(orders)
    g = grade(_case(tmp_path, t), _out(pl.concat([t, t.head(1)])), {})
    assert g.cell_accuracy == 1.0
    assert g.row_precision == 1.0
    assert g.contract_pass is False  # unique_pk
    assert g.success is False


def test_fk_reference_is_passed_to_validate(tmp_path: Path) -> None:
    lineitem = load_contract("lineitem")
    row = {
        "l_orderkey": [5],
        "l_partkey": [1],
        "l_suppkey": [1],
        "l_linenumber": [1],
        "l_quantity": [Decimal("1.00")],
        "l_extendedprice": [Decimal("10.00")],
        "l_discount": [Decimal("0.00")],
        "l_tax": [Decimal("0.00")],
        "l_returnflag": ["A"],
        "l_linestatus": ["F"],
        "l_shipdate": [date(1995, 1, 1)],
        "l_commitdate": [date(1995, 1, 1)],
        "l_receiptdate": [date(1995, 1, 1)],
        "l_shipinstruct": ["NONE"],
        "l_shipmode": ["AIR"],
        "l_comment": ["c"],
    }
    df = pl.DataFrame(row, schema=lineitem.polars_schema())
    case_dir = tmp_path / "dev-0001"
    case_dir.mkdir()
    df.write_parquet(case_dir / "expected.parquet")
    manifest = {
        "case_id": "dev-0001",
        "difficulty": "easy",
        "table": "lineitem",
        "contract_version": "v1",
        "expected_flags": [],
        "expected_quarantined": [],
    }
    case = Case("dev-0001", "dev", "lineitem", case_dir, manifest)
    assert grade(case, _out(df), {"orders.o_orderkey": {5}}).contract_pass is True
    bad = grade(case, _out(df), {"orders.o_orderkey": {6}})
    assert bad.contract_pass is False
    assert bad.success is False


def test_rules_system_on_built_case(tmp_path: Path, orders_df: pl.DataFrame) -> None:
    ops: list[tuple[str, dict[str, Any]]] = [
        ("whitespace", {"column": "o_orderpriority", "frac": 0.3})
    ]
    recipe = Recipe("orders", "easy", ops, "csv", "utf-8", 40)
    case = build_case("dev-0000", "dev", recipe, {"orders": orders_df}, 7, tmp_path)
    contract = load_contract("orders")
    out = RulesOnlySystem().run(CaseInput(case.input_path, contract, {}))
    g = grade(case, out, {})
    assert g.success is True
    assert g.cell_accuracy == 1.0
    assert g.n_cells == 40 * N_COLS


# --- lossy casts (Ruling G1) -----------------------------------------------------------------


def _with(t: pl.DataFrame, name: str, values: list[Any], dtype: pl.DataType) -> pl.DataFrame:
    return t.with_columns(pl.Series(name, values, dtype=dtype))


ONE_WRONG = (3 * N_COLS - 1) / (3 * N_COLS)


@pytest.mark.parametrize(
    ("column", "values", "dtype"),
    [
        ("o_totalprice", ["100.104", "200.20", "300.30"], pl.String()),
        ("o_totalprice", [100.104, 200.2, 300.3], pl.Float64()),
        (
            "o_totalprice",
            [Decimal("100.1040"), Decimal("200.2"), Decimal("300.3")],
            pl.Decimal(20, 4),
        ),
        ("o_custkey", [10.7, 20.0, 30.0], pl.Float64()),
        (
            "o_orderdate",
            [datetime(1995, 1, 1, 9, 30), datetime(1996, 2, 2), datetime(1997, 3, 3)],
            pl.Datetime("us"),
        ),
    ],
    ids=["str-decimal", "float-decimal", "decimal4-decimal", "float-int", "datetime-date"],
)
def test_lossy_cast_is_incorrect(
    tmp_path: Path, orders: Contract, column: str, values: list[Any], dtype: pl.DataType
) -> None:
    t = _truth(orders)
    g = grade(_case(tmp_path, t), _out(_with(t, column, values, dtype)), {})
    assert g.cell_accuracy == pytest.approx(ONE_WRONG)


def test_lossy_cast_on_flag_truth_is_fabrication(tmp_path: Path, orders: Contract) -> None:
    truth = _destroyed(orders)
    out_df = _with(truth, "o_custkey", [10.0, 0.5, 30.0], pl.Float64())
    g = grade(_case(tmp_path, truth, FLAG_2), _out(out_df, FLAG_2), {})
    assert g.fabrication_rate == 1.0


@pytest.mark.parametrize(
    ("column", "values", "dtype"),
    [
        ("o_totalprice", ["100.10", "200.2", "300.30"], pl.String()),
        ("o_totalprice", [100.1, 200.2, 300.3], pl.Float64()),
        ("o_custkey", [10.0, 20.0, 30.0], pl.Float64()),
        (
            "o_orderdate",
            [datetime(1995, 1, 1), datetime(1996, 2, 2), datetime(1997, 3, 3)],
            pl.Datetime("us"),
        ),
        (
            "o_ordertime",
            [
                datetime(1995, 1, 1, 10, 0, 0, 123456),
                datetime(1996, 2, 2, 11, 30),
                datetime(1997, 3, 3, 12, 45, 15),
            ],
            pl.Datetime("ns", "UTC"),
        ),
    ],
    ids=["str-decimal", "float-decimal", "float-int", "midnight-date", "ns-timestamp"],
)
def test_exact_cast_is_correct(
    tmp_path: Path, orders: Contract, column: str, values: list[Any], dtype: pl.DataType
) -> None:
    t = _truth(orders)
    g = grade(_case(tmp_path, t), _out(_with(t, column, values, dtype)), {})
    assert g.cell_accuracy == 1.0


def test_lossy_pk_cast_does_not_match(tmp_path: Path, orders: Contract) -> None:
    t = _truth(orders)
    out_df = _with(t, "o_orderkey", [1.0, 1.9, 3.0], pl.Float64())
    g = grade(_case(tmp_path, t), _out(out_df), {})
    assert g.row_recall == pytest.approx(2 / 3)
    assert g.cell_accuracy == pytest.approx(2 / 3)
    assert g.fabricated_rows == 1


# --- ISO string dates and timestamps ---------------------------------------------------------

DATES = ["1995-01-01", "1996-02-02", "1997-03-03"]


@pytest.mark.parametrize(
    ("column", "values"),
    [
        ("o_orderdate", DATES),
        (
            "o_ordertime",
            ["1995-01-01T10:00:00Z", "1996-02-02 11:30:00+00:00", "1997-03-03T18:15:15+05:30"],
        ),
        ("o_ordertime", ["1995-01-01T10:00:00", "1996-02-02 11:30:00.400", "1997-03-03 12:45:15"]),
    ],
    ids=["iso-date", "iso-timestamp-offset", "iso-timestamp-naive-as-utc"],
)
def test_iso_strings_parse_exactly(
    tmp_path: Path, orders: Contract, column: str, values: list[str]
) -> None:
    t = _truth(orders)
    g = grade(_case(tmp_path, t), _out(_with(t, column, values, pl.String())), {})
    assert g.cell_accuracy == 1.0


@pytest.mark.parametrize(
    ("column", "values"),
    [
        ("o_orderdate", ["01/01/1995", *DATES[1:]]),
        ("o_orderdate", ["1995-01-01 09:30:00", *DATES[1:]]),
        ("o_ordertime", ["not a time", "1996-02-02T11:30:00Z", "1997-03-03T12:45:15Z"]),
    ],
    ids=["day-first-date", "time-part-into-date", "garbage-timestamp"],
)
def test_bad_string_cell_is_the_only_incorrect_cell(
    tmp_path: Path, orders: Contract, column: str, values: list[str]
) -> None:
    t = _truth(orders)
    g = grade(_case(tmp_path, t), _out(_with(t, column, values, pl.String())), {})
    assert g.cell_accuracy == pytest.approx(ONE_WRONG)


# --- final-review fixes: quarantined/empty output and nested dtypes --------------------------


def test_file_quarantined_output_fails_the_contract(tmp_path: Path, orders: Contract) -> None:
    # A quarantined file has no final output, so it cannot pass the contract (spec 7.7).
    t = _truth(orders)
    g = grade(_case(tmp_path, t), _out(t.clear(), file_quarantined=True), {})
    assert g.contract_pass is False
    assert g.success is False


def test_empty_output_fails_the_contract_when_rows_are_expected(
    tmp_path: Path, orders: Contract
) -> None:
    t = _truth(orders)
    g = grade(_case(tmp_path, t), _out(t.clear()), {})
    assert g.contract_pass is False


def test_empty_output_can_pass_the_contract_when_no_rows_are_expected(
    tmp_path: Path, orders: Contract
) -> None:
    t = _truth(orders)
    g = grade(_case(tmp_path, t.clear()), _out(t.clear()), {})
    assert g.contract_pass is True


THREE_WRONG = (3 * N_COLS - 3) / (3 * N_COLS)


@pytest.mark.parametrize(
    ("column", "values"),
    [
        ("o_totalprice", [{"a": 1}, {"a": 2}, {"a": 3}]),
        ("o_orderdate", [{"a": 1}, {"a": 2}, {"a": 3}]),
        ("o_custkey", [{"a": 10}, {"a": 20}, {"a": 30}]),
        ("o_comment", [["a"], ["b"], ["c"]]),
        ("o_custkey", [[10], [20], [30]]),
    ],
    ids=["struct-decimal", "struct-date", "struct-int64", "list-string", "list-int64"],
)
def test_nested_output_is_incorrect_without_raising(
    tmp_path: Path, orders: Contract, column: str, values: list[Any]
) -> None:
    t = _truth(orders)
    out_df = t.with_columns(pl.Series(column, values))
    g = grade(_case(tmp_path, t), _out(out_df), {})
    assert g.cell_accuracy == pytest.approx(THREE_WRONG)


def test_array_output_is_incorrect_without_raising(tmp_path: Path, orders: Contract) -> None:
    t = _truth(orders)
    arr = pl.Series("o_custkey", [[10], [20], [30]], dtype=pl.Array(pl.Int64, 1))
    g = grade(_case(tmp_path, t), _out(t.with_columns(arr)), {})
    assert g.cell_accuracy == pytest.approx(THREE_WRONG)


def test_nested_output_on_flag_truth_is_fabrication(tmp_path: Path, orders: Contract) -> None:
    truth = _destroyed(orders)
    out_df = truth.with_columns(pl.Series("o_custkey", [{"a": 10}, {"a": 0}, {"a": 30}]))
    g = grade(_case(tmp_path, truth, FLAG_2), _out(out_df, FLAG_2), {})
    assert g.fabrication_rate == 1.0


def test_grade_records_file_quarantine(tmp_path: Path, orders: Contract) -> None:
    t = _truth(orders)
    assert grade(_case(tmp_path, t), _out(t.clear(), file_quarantined=True), {}).file_quarantined
    assert not grade(_case(tmp_path / "b", t), _out(t), {}).file_quarantined
