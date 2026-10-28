from decimal import Decimal

import polars as pl
import pytest

from sluice.contracts.model import ColumnSpec, Contract, load_contract
from sluice.contracts.validate import ValidationReport, Violation, validate

ORDERS_COLUMNS = [
    "o_orderkey",
    "o_custkey",
    "o_orderstatus",
    "o_totalprice",
    "o_orderdate",
    "o_ordertime",
    "o_orderpriority",
    "o_clerk",
    "o_shippriority",
    "o_comment",
]


def _only(report: ValidationReport, check: str, column: str | None = None) -> Violation:
    matches = [v for v in report.violations if v.check == check and v.column == column]
    assert len(matches) == 1, report.violations
    return matches[0]


def _set_at(df: pl.DataFrame, col: str, key: int, value: object) -> pl.DataFrame:
    """Return a copy of orders `df` with `col` set to `value` on the row o_orderkey == key."""
    dtype = df.schema[col]
    return df.with_columns(
        pl.when(pl.col("o_orderkey") == key)
        .then(pl.lit(value, dtype=dtype))
        .otherwise(pl.col(col))
        .alias(col)
    )


def _orders_ref(orders_df: pl.DataFrame) -> dict[str, set]:
    return {"orders.o_orderkey": set(orders_df["o_orderkey"].to_list())}


# --- contract model ---------------------------------------------------------------------------


def test_load_orders_contract_values() -> None:
    c = load_contract("orders")
    assert (c.name, c.version, c.locale) == ("orders", "v1", "en_IN")
    assert c.primary_key == ("o_orderkey",)
    assert [col.name for col in c.columns] == ORDERS_COLUMNS
    assert c.foreign_keys == {}
    price = c.column("o_totalprice")
    assert (price.dtype, price.unit, price.min, price.max) == ("decimal", "INR", 0, 600000)
    assert c.column("o_orderstatus").enum == ["F", "O", "P"]
    assert c.column("o_orderpriority").enum == [
        "1-URGENT",
        "2-HIGH",
        "3-MEDIUM",
        "4-NOT SPECIFIED",
        "5-LOW",
    ]
    assert c.column("o_ordertime").dtype == "timestamp_utc"
    assert c.column("o_shippriority").dtype == "int32"


def test_load_lineitem_contract_values() -> None:
    c = load_contract("lineitem", "v1")
    assert c.primary_key == ("l_orderkey", "l_linenumber")
    assert c.foreign_keys == {"l_orderkey": ("orders", "o_orderkey")}
    assert c.column("l_returnflag").enum == ["A", "N", "R"]
    assert c.column("l_linestatus").enum == ["F", "O"]
    ext = c.column("l_extendedprice")
    assert (ext.dtype, ext.unit, ext.min, ext.max) == ("decimal", "INR", 0, None)


def test_polars_schema_maps_dtypes() -> None:
    schema = load_contract("orders").polars_schema()
    assert list(schema) == ORDERS_COLUMNS
    assert schema["o_orderkey"] == pl.Int64
    assert schema["o_shippriority"] == pl.Int32
    assert schema["o_totalprice"] == pl.Decimal(15, 2)
    assert schema["o_orderdate"] == pl.Date
    assert schema["o_ordertime"] == pl.Datetime("us", "UTC")
    assert schema["o_clerk"] == pl.String


def test_contract_column_unknown_raises() -> None:
    with pytest.raises(KeyError, match="nope"):
        load_contract("orders").column("nope")


@pytest.mark.parametrize("name", ["../secrets", "orders/../x", "", "Orders!"])
def test_load_contract_rejects_unsafe_names(name: str) -> None:
    with pytest.raises(ValueError, match="contract"):
        load_contract(name)


def test_load_contract_missing_raises() -> None:
    with pytest.raises(FileNotFoundError):
        load_contract("orders", "v99")


def test_contract_rejects_pk_not_in_columns() -> None:
    with pytest.raises(ValueError, match="primary key"):
        Contract(
            name="t",
            version="v1",
            primary_key=("missing",),
            columns=(ColumnSpec(name="a", dtype="int64"),),
            foreign_keys={},
            locale="en_IN",
        )


def test_columnspec_rejects_min_above_max() -> None:
    with pytest.raises(ValueError, match="min"):
        ColumnSpec(name="a", dtype="int64", min=5, max=1)


# --- validate: clean data ---------------------------------------------------------------------


def test_clean_orders_pass(orders_df: pl.DataFrame) -> None:
    report = validate(load_contract("orders"), orders_df)
    assert report.violations == []
    assert report.passed is True


def test_clean_lineitem_pass_with_reference(
    orders_df: pl.DataFrame, lineitem_df: pl.DataFrame
) -> None:
    report = validate(load_contract("lineitem"), lineitem_df, reference=_orders_ref(orders_df))
    assert report.violations == []
    assert report.passed is True


# --- validate: each check ---------------------------------------------------------------------


def test_negative_price_fails(orders_df: pl.DataFrame) -> None:
    bad = _set_at(orders_df, "o_totalprice", 7, Decimal("-1.00"))
    report = validate(load_contract("orders"), bad)
    assert report.passed is False
    v = _only(report, "min", "o_totalprice")
    assert v.pk_values == [(7,)]
    assert "o_totalprice" in v.message
    assert "min" in v.message
    assert "7" in v.message


def test_price_above_max_fails(orders_df: pl.DataFrame) -> None:
    bad = _set_at(orders_df, "o_totalprice", 1, Decimal("600000.01"))
    v = _only(validate(load_contract("orders"), bad), "max", "o_totalprice")
    assert v.pk_values == [(1,)]


def test_price_at_bounds_passes(orders_df: pl.DataFrame) -> None:
    bad = _set_at(orders_df, "o_totalprice", 1, Decimal("600000.00"))
    bad = _set_at(bad, "o_totalprice", 2, Decimal("0.00"))
    assert validate(load_contract("orders"), bad).passed is True


def test_missing_column_fails(orders_df: pl.DataFrame) -> None:
    report = validate(load_contract("orders"), orders_df.drop("o_clerk"))
    assert report.passed is False
    v = _only(report, "missing_column", "o_clerk")
    assert v.pk_values == []
    assert "o_clerk" in v.message


def test_extra_column_fails(orders_df: pl.DataFrame) -> None:
    report = validate(load_contract("orders"), orders_df.with_columns(junk=pl.lit(1)))
    assert report.passed is False
    v = _only(report, "extra_column", "junk")
    assert "junk" in v.message


def test_dtype_mismatch_fails(orders_df: pl.DataFrame) -> None:
    bad = orders_df.with_columns(pl.col("o_totalprice").cast(pl.String))
    report = validate(load_contract("orders"), bad)
    assert report.passed is False
    v = _only(report, "dtype", "o_totalprice")
    assert "o_totalprice" in v.message
    # value checks on a mistyped column would compare strings lexically: suppressed
    assert [x.check for x in report.violations if x.column == "o_totalprice"] == ["dtype"]


def test_float_money_is_a_dtype_violation(orders_df: pl.DataFrame) -> None:
    bad = orders_df.with_columns(pl.col("o_totalprice").cast(pl.Float64))
    _only(validate(load_contract("orders"), bad), "dtype", "o_totalprice")


def test_naive_ordertime_is_a_dtype_violation(orders_df: pl.DataFrame) -> None:
    bad = orders_df.with_columns(pl.col("o_ordertime").dt.replace_time_zone(None))
    _only(validate(load_contract("orders"), bad), "dtype", "o_ordertime")


def test_enum_violation(orders_df: pl.DataFrame) -> None:
    bad = _set_at(orders_df, "o_orderstatus", 5, "X")
    v = _only(validate(load_contract("orders"), bad), "enum", "o_orderstatus")
    assert v.pk_values == [(5,)]


def test_null_clerk_fails_not_null(orders_df: pl.DataFrame) -> None:
    bad = _set_at(orders_df, "o_clerk", 4, None)
    v = _only(validate(load_contract("orders"), bad), "not_null", "o_clerk")
    assert v.pk_values == [(4,)]


def test_flagged_null_clerk_is_exempt(orders_df: pl.DataFrame) -> None:
    bad = _set_at(orders_df, "o_clerk", 4, None)
    report = validate(load_contract("orders"), bad, flags={((4,), "o_clerk")})
    assert report.violations == []
    assert report.passed is True


def test_flag_on_other_column_does_not_exempt(orders_df: pl.DataFrame) -> None:
    bad = _set_at(orders_df, "o_clerk", 4, None)
    report = validate(load_contract("orders"), bad, flags={((4,), "o_comment")})
    _only(report, "not_null", "o_clerk")


def test_duplicate_pk_fails(orders_df: pl.DataFrame) -> None:
    dup = pl.concat([orders_df, orders_df.filter(pl.col("o_orderkey") == 1)])
    v = _only(validate(load_contract("orders"), dup), "unique_pk", None)
    assert v.pk_values == [(1,)]


def test_fk_violation(orders_df: pl.DataFrame, lineitem_df: pl.DataFrame) -> None:
    first = lineitem_df.row(0, named=True)
    bad = lineitem_df.with_columns(
        pl.when((pl.col("l_orderkey") == first["l_orderkey"]) & (pl.col("l_linenumber") == 1))
        .then(pl.lit(-5, dtype=pl.Int64))
        .otherwise(pl.col("l_orderkey"))
        .alias("l_orderkey")
    )
    report = validate(load_contract("lineitem"), bad, reference=_orders_ref(orders_df))
    assert report.passed is False
    v = _only(report, "foreign_key", "l_orderkey")
    assert (-5, 1) in v.pk_values
    assert "orders.o_orderkey" in v.message


def test_fk_skipped_without_reference(lineitem_df: pl.DataFrame) -> None:
    bad = lineitem_df.with_columns(pl.lit(-5, dtype=pl.Int64).alias("l_orderkey"))
    report = validate(load_contract("lineitem"), bad)
    assert not [v for v in report.violations if v.check == "foreign_key"]


def test_pk_values_are_python_native(orders_df: pl.DataFrame) -> None:
    bad = _set_at(orders_df, "o_totalprice", 7, Decimal("-1.00"))
    v = _only(validate(load_contract("orders"), bad), "min", "o_totalprice")
    assert all(type(x) is int for pk in v.pk_values for x in pk)


def test_message_lists_at_most_five_pks(orders_df: pl.DataFrame) -> None:
    bad = orders_df.with_columns(pl.lit("X").alias("o_orderstatus"))
    v = _only(validate(load_contract("orders"), bad), "enum", "o_orderstatus")
    assert len(v.pk_values) == orders_df.height
    shown = [str(pk) for pk in v.pk_values[:6]]
    assert all(s in v.message for s in shown[:5])
    assert shown[5] not in v.message


def test_empty_frame_with_right_schema_passes(orders_df: pl.DataFrame) -> None:
    assert validate(load_contract("orders"), orders_df.head(0)).passed is True


def test_violation_order_is_deterministic(orders_df: pl.DataFrame) -> None:
    bad = _set_at(orders_df, "o_totalprice", 7, Decimal("-1.00"))
    bad = _set_at(bad, "o_orderstatus", 7, "X").with_columns(junk=pl.lit(1)).drop("o_comment")
    checks = [(v.column, v.check) for v in validate(load_contract("orders"), bad).violations]
    assert checks == [
        ("o_comment", "missing_column"),
        ("junk", "extra_column"),
        ("o_orderstatus", "enum"),
        ("o_totalprice", "min"),
    ]


def test_not_null_reported_when_pk_column_missing(orders_df: pl.DataFrame) -> None:
    bad = _set_at(orders_df, "o_clerk", 4, None).drop("o_orderkey")
    report = validate(load_contract("orders"), bad)
    _only(report, "missing_column", "o_orderkey")
    v = _only(report, "not_null", "o_clerk")
    assert v.pk_values == []
    assert "1 row(s)" in v.message


def test_foreign_key_reported_when_pk_column_missing(
    orders_df: pl.DataFrame, lineitem_df: pl.DataFrame
) -> None:
    first = lineitem_df.row(0, named=True)
    bad = lineitem_df.with_columns(
        pl.when((pl.col("l_orderkey") == first["l_orderkey"]) & (pl.col("l_linenumber") == 1))
        .then(pl.lit(-5, dtype=pl.Int64))
        .otherwise(pl.col("l_orderkey"))
        .alias("l_orderkey")
    ).drop("l_linenumber")
    report = validate(load_contract("lineitem"), bad, reference=_orders_ref(orders_df))
    _only(report, "missing_column", "l_linenumber")
    v = _only(report, "foreign_key", "l_orderkey")
    assert v.pk_values == []
    assert "1 row(s)" in v.message


def test_validate_does_not_mutate_input(orders_df: pl.DataFrame) -> None:
    before = orders_df.clone()
    validate(load_contract("orders"), orders_df.drop("o_clerk"))
    assert orders_df.equals(before)
