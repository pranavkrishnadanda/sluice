"""Unit tests (no services): contract -> Iceberg schema and Polars -> Arrow conversion."""

from datetime import UTC, date, datetime
from decimal import Decimal

import polars as pl
import pyarrow as pa
import pytest
from pyiceberg.types import (
    DateType,
    DecimalType,
    IntegerType,
    LongType,
    StringType,
    TimestamptzType,
)

from sluice.contracts.model import ColumnSpec, Contract, load_contract
from sluice.load.iceberg import (
    LoadError,
    arrow_schema,
    iceberg_schema,
    iceberg_type,
    pk_text,
    quarantine_schema,
    to_arrow,
)


def _contract() -> Contract:
    return Contract(
        name="t",
        version="v1",
        primary_key=("k",),
        columns=(
            ColumnSpec("k", "int64"),
            ColumnSpec("n", "int32"),
            ColumnSpec("amount", "decimal"),
            ColumnSpec("label", "string"),
            ColumnSpec("day", "date"),
            ColumnSpec("at", "timestamp_utc"),
        ),
    )


def _frame(contract: Contract, **overrides: list[object]) -> pl.DataFrame:
    data: dict[str, list[object]] = {
        "k": [1, 2],
        "n": [7, None],
        "amount": [Decimal("1234567890123.45"), Decimal("0.10")],
        "label": ["a", None],
        "day": [date(1995, 3, 15), None],
        "at": [datetime(1995, 3, 15, 10, 14, 3, 123456, tzinfo=UTC), None],
    }
    data.update(overrides)
    return pl.DataFrame(data, schema=contract.polars_schema())


@pytest.mark.parametrize(
    ("dtype", "expected"),
    [
        ("int64", LongType()),
        ("int32", IntegerType()),
        ("decimal", DecimalType(15, 2)),
        ("string", StringType()),
        ("date", DateType()),
        ("timestamp_utc", TimestamptzType()),
    ],
)
def test_iceberg_type_maps_each_contract_dtype_exactly(dtype: str, expected: object) -> None:
    assert iceberg_type(dtype) == expected  # type: ignore[arg-type]


def test_iceberg_type_unknown_dtype_raises() -> None:
    with pytest.raises(LoadError, match="float"):
        iceberg_type("float")  # type: ignore[arg-type]


def test_iceberg_schema_keeps_contract_order_and_ids() -> None:
    schema = iceberg_schema(_contract())
    assert [f.name for f in schema.fields] == ["k", "n", "amount", "label", "day", "at"]
    assert [f.field_id for f in schema.fields] == [1, 2, 3, 4, 5, 6]


def test_iceberg_schema_pk_required_other_columns_optional() -> None:
    # Flagged cells are null in the loaded table, so only key columns can be required.
    schema = iceberg_schema(_contract())
    assert {f.name: f.required for f in schema.fields} == {
        "k": True,
        "n": False,
        "amount": False,
        "label": False,
        "day": False,
        "at": False,
    }
    assert list(schema.identifier_field_ids) == [1]


def test_iceberg_schema_for_real_contracts_builds() -> None:
    for name in ("orders", "lineitem"):
        contract = load_contract(name)
        assert len(iceberg_schema(contract).fields) == len(contract.columns)


def test_quarantine_schema_has_pk_text_and_reason() -> None:
    fields = {f.name: (f.field_type, f.required) for f in quarantine_schema().fields}
    assert fields["pk"] == (StringType(), True)
    assert fields["reason"] == (StringType(), True)


def test_arrow_schema_types_are_exact() -> None:
    schema = arrow_schema(_contract())
    assert schema.field("k").type == pa.int64()
    assert schema.field("n").type == pa.int32()
    assert schema.field("amount").type == pa.decimal128(15, 2)
    assert schema.field("label").type == pa.large_string()
    assert schema.field("day").type == pa.date32()
    assert schema.field("at").type == pa.timestamp("us", tz="UTC")
    assert schema.field("k").nullable is False
    assert schema.field("amount").nullable is True


def test_to_arrow_preserves_decimal_money_exactly() -> None:
    contract = _contract()
    table = to_arrow(_frame(contract), contract)
    assert table.column("amount").type == pa.decimal128(15, 2)
    assert table.column("amount").to_pylist() == [Decimal("1234567890123.45"), Decimal("0.10")]


def test_to_arrow_preserves_timestamp_microseconds_and_utc() -> None:
    contract = _contract()
    table = to_arrow(_frame(contract), contract)
    assert table.column("at").to_pylist()[0] == datetime(1995, 3, 15, 10, 14, 3, 123456, tzinfo=UTC)


def test_to_arrow_keeps_nulls_for_flagged_cells() -> None:
    contract = _contract()
    table = to_arrow(_frame(contract), contract)
    assert table.column("label").null_count == 1
    assert table.column("day").to_pylist() == [date(1995, 3, 15), None]


def test_to_arrow_result_matches_arrow_schema() -> None:
    contract = _contract()
    assert to_arrow(_frame(contract), contract).schema.equals(arrow_schema(contract))


def test_to_arrow_rejects_float_money() -> None:
    contract = _contract()
    df = _frame(contract).with_columns(pl.col("amount").cast(pl.Float64))
    with pytest.raises(LoadError, match="amount"):
        to_arrow(df, contract)


def test_to_arrow_rejects_wrong_column_order() -> None:
    contract = _contract()
    df = _frame(contract).select(["n", "k", "amount", "label", "day", "at"])
    with pytest.raises(LoadError, match="schema"):
        to_arrow(df, contract)


def test_to_arrow_rejects_null_primary_key() -> None:
    contract = _contract()
    df = _frame(contract).with_columns(pl.lit(None, dtype=pl.Int64).alias("k"))
    with pytest.raises(LoadError, match="null"):
        to_arrow(df, contract)


def test_to_arrow_rejects_duplicate_primary_key() -> None:
    contract = _contract()
    df = _frame(contract, k=[1, 1])
    with pytest.raises(LoadError, match="duplicate"):
        to_arrow(df, contract)


def test_to_arrow_empty_frame_gives_empty_table() -> None:
    contract = _contract()
    table = to_arrow(pl.DataFrame(schema=contract.polars_schema()), contract)
    assert table.num_rows == 0
    assert table.schema.equals(arrow_schema(contract))


def test_pk_text_is_compact_json_array() -> None:
    assert pk_text((1,)) == "[1]"
    assert pk_text((7, 3)) == "[7,3]"
    assert pk_text(('a"b',)) == '["a\\"b"]'
