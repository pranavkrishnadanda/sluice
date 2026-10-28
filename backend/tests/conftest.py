from pathlib import Path

import polars as pl
import pytest

from sluice.data.tpch import generate_tpch

TPCH_SF = 0.01


@pytest.fixture(scope="session")
def tpch_paths(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    """TPC-H at sf=0.01, generated once per session (a few seconds). Do not modify the files."""
    return generate_tpch(TPCH_SF, tmp_path_factory.mktemp("tpch"))


@pytest.fixture(scope="session")
def orders_df(tpch_paths: dict[str, Path]) -> pl.DataFrame:
    """Shared clean orders frame. Derive modified copies (with_columns, ...); never mutate."""
    return pl.read_parquet(tpch_paths["orders"])


@pytest.fixture(scope="session")
def lineitem_df(tpch_paths: dict[str, Path]) -> pl.DataFrame:
    """Shared clean lineitem frame. Derive modified copies; never mutate."""
    return pl.read_parquet(tpch_paths["lineitem"])
