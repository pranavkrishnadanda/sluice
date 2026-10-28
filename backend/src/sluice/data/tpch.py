"""Deterministic TPC-H ground truth generated with DuckDB's ``tpch`` extension.

``dbgen`` is deterministic for a given scale factor; rows are additionally ordered by primary
key before writing so the Parquet bytes are reproducible run to run.
"""

from pathlib import Path

import duckdb
import polars as pl

TABLES: dict[str, str] = {
    "orders": "SELECT * FROM orders ORDER BY o_orderkey",
    "lineitem": "SELECT * FROM lineitem ORDER BY l_orderkey, l_linenumber",
}

# o_ordertime = o_orderdate at 00:00 UTC + (o_orderkey * ORDERTIME_PRIME) % SECONDS_PER_DAY.
ORDERTIME_PRIME = 7919
SECONDS_PER_DAY = 86_400


def _add_ordertime(orders: pl.DataFrame) -> pl.DataFrame:
    """Insert the derived ``o_ordertime`` (UTC, microseconds) right after ``o_orderdate``."""
    offset = pl.duration(seconds=(pl.col("o_orderkey") * ORDERTIME_PRIME) % SECONDS_PER_DAY)
    ordertime = (
        pl.col("o_orderdate").cast(pl.Datetime("us")).dt.replace_time_zone("UTC") + offset
    ).alias("o_ordertime")
    out = orders.with_columns(ordertime)
    cols = list(orders.columns)
    cols.insert(cols.index("o_orderdate") + 1, "o_ordertime")
    return out.select(cols)


def generate_tpch(sf: float, out_dir: Path) -> dict[str, Path]:
    """Generate TPC-H ``orders`` and ``lineitem`` at scale factor ``sf`` as Parquet.

    Args:
        sf: TPC-H scale factor (> 0). ``0.01`` gives 15000 orders and 60175 line items.
        out_dir: Directory to write into; created if missing. Existing files are overwritten.

    Returns:
        Mapping of table name (``"orders"``, ``"lineitem"``) to the written Parquet path.

    Raises:
        ValueError: If ``sf`` is not positive.
    """
    if not sf > 0:
        raise ValueError(f"sf must be > 0, got {sf!r}")
    out_dir.mkdir(parents=True, exist_ok=True)
    paths: dict[str, Path] = {}
    with duckdb.connect(":memory:") as con:
        con.execute("INSTALL tpch")
        con.execute("LOAD tpch")
        con.execute("CALL dbgen(sf = ?)", [float(sf)])
        for table, query in TABLES.items():
            df = con.sql(query).pl()
            if table == "orders":
                df = _add_ordertime(df)
            path = out_dir / f"{table}.parquet"
            df.write_parquet(path)
            paths[table] = path
    return paths
