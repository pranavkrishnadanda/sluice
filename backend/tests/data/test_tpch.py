import datetime as dt
import hashlib
from pathlib import Path

import polars as pl
import pytest

from sluice.data.tpch import generate_tpch

ORDERS_SF001 = 15000
LINEITEM_SF001 = 60175


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_tpch_counts(tpch_paths: dict[str, Path]) -> None:
    assert set(tpch_paths) == {"orders", "lineitem"}
    assert pl.read_parquet(tpch_paths["orders"]).height == ORDERS_SF001
    assert pl.read_parquet(tpch_paths["lineitem"]).height == LINEITEM_SF001


def test_tpch_writes_into_out_dir(tpch_paths: dict[str, Path]) -> None:
    assert tpch_paths["orders"].name == "orders.parquet"
    assert tpch_paths["lineitem"].name == "lineitem.parquet"
    assert tpch_paths["orders"].parent == tpch_paths["lineitem"].parent


def test_tpch_deterministic(tpch_paths: dict[str, Path], tmp_path: Path) -> None:
    second = generate_tpch(0.01, tmp_path / "again")
    assert _sha256(second["orders"]) == _sha256(tpch_paths["orders"])
    assert _sha256(second["lineitem"]) == _sha256(tpch_paths["lineitem"])


def test_tpch_money_is_decimal_15_2(orders_df: pl.DataFrame, lineitem_df: pl.DataFrame) -> None:
    assert orders_df.schema["o_totalprice"] == pl.Decimal(15, 2)
    assert lineitem_df.schema["l_extendedprice"] == pl.Decimal(15, 2)


def test_ordertime_derived(orders_df: pl.DataFrame) -> None:
    assert orders_df.schema["o_ordertime"] == pl.Datetime("us", "UTC")
    assert orders_df["o_ordertime"].null_count() == 0
    assert (orders_df["o_ordertime"].dt.date() == orders_df["o_orderdate"]).all()


def test_ordertime_formula(orders_df: pl.DataFrame) -> None:
    row = orders_df.filter(pl.col("o_orderkey") == 3).row(0, named=True)
    seconds = (3 * 7919) % 86400
    midnight = dt.datetime.combine(row["o_orderdate"], dt.time(), tzinfo=dt.UTC)
    assert row["o_ordertime"] == midnight + dt.timedelta(seconds=seconds)


def test_ordertime_follows_orderdate(orders_df: pl.DataFrame) -> None:
    cols = orders_df.columns
    assert cols.index("o_ordertime") == cols.index("o_orderdate") + 1


def test_tpch_rejects_non_positive_sf(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="sf"):
        generate_tpch(0, tmp_path)
