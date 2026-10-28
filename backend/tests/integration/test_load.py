"""Iceberg load + lineage against the Compose stack. Run with `make test-integration`.

Each test loads into its own Iceberg namespace and uses a random file hash, so tests never see
each other's snapshots or idempotency records.
"""

import secrets
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import polars as pl
import psycopg
import pytest
from pyiceberg.table import Table
from typer.testing import CliRunner

from sluice import cli
from sluice.contracts.model import Contract, load_contract
from sluice.evals.types import SystemOutput
from sluice.load import iceberg
from sluice.load.iceberg import LoadResult, load_output, rollback
from sluice.settings import Settings, get_settings

pytestmark = pytest.mark.integration


@pytest.fixture
def settings() -> Settings:
    return get_settings()


@pytest.fixture
def ns() -> str:
    return f"t_{secrets.token_hex(4)}"


@pytest.fixture
def orders() -> Contract:
    return load_contract("orders")


@pytest.fixture
def clean(orders_df: pl.DataFrame, orders: Contract) -> pl.DataFrame:
    schema = orders.polars_schema()
    return orders_df.head(50).select(list(schema)).cast(schema)  # type: ignore[arg-type]


def _sha() -> str:
    return secrets.token_hex(32)


def _out(df: pl.DataFrame, **kw: Any) -> SystemOutput:
    base: dict[str, Any] = {
        "df": df,
        "flags": set(),
        "quarantined": set(),
        "file_quarantined": False,
        "cost_usd": 0.0,
        "latency_s": 0.5,
        "meta": {},
    }
    base.update(kw)
    return SystemOutput(**base)


def _load(
    out: SystemOutput, contract: Contract, settings: Settings, ns: str, sha: str, run_id: str = ""
) -> LoadResult:
    return load_output(
        out,
        contract,
        run_id=run_id or secrets.token_hex(16),
        file_sha256=sha,
        settings=settings,
        namespace=ns,
    )


@pytest.fixture
def table_of(settings: Settings, ns: str) -> Iterator[Any]:
    with iceberg.catalog(settings) as cat:

        def get(name: str) -> Table:
            return cat.load_table(f"{ns}.{name}")

        yield get


def _rows(conn: psycopg.Connection, sql: str, *params: object) -> list[tuple[Any, ...]]:
    return conn.execute(sql, params).fetchall()


@pytest.fixture
def pg(settings: Settings) -> Iterator[psycopg.Connection]:
    with psycopg.connect(settings.postgres_dsn, connect_timeout=5, autocommit=True) as conn:
        yield conn


def test_clean_orders_frame_creates_snapshot_with_its_rows(
    clean: pl.DataFrame, orders: Contract, settings: Settings, ns: str, table_of: Any
) -> None:
    result = _load(_out(clean), orders, settings, ns, _sha())

    table = table_of("orders")
    assert result.skipped is False
    assert result.rows_loaded == 50
    assert result.snapshot_id == str(table.current_snapshot().snapshot_id)
    loaded = pl.from_arrow(table.scan().to_arrow()).sort("o_orderkey")
    assert loaded.cast(orders.polars_schema()).equals(clean.sort("o_orderkey"))  # type: ignore[arg-type]


def test_loaded_money_is_exact_decimal(
    clean: pl.DataFrame, orders: Contract, settings: Settings, ns: str, table_of: Any
) -> None:
    _load(_out(clean), orders, settings, ns, _sha())
    field = table_of("orders").schema().find_field("o_totalprice")
    assert str(field.field_type) == "decimal(15, 2)"
    arrow = table_of("orders").scan().to_arrow()
    assert str(arrow.schema.field("o_totalprice").type) == "decimal128(15, 2)"


def test_same_file_again_is_skipped_and_writes_nothing(
    clean: pl.DataFrame, orders: Contract, settings: Settings, ns: str, table_of: Any
) -> None:
    sha = _sha()
    first = _load(_out(clean), orders, settings, ns, sha)
    n_snapshots = len(table_of("orders").snapshots())

    again = _load(_out(clean), orders, settings, ns, sha)

    table = table_of("orders")
    assert again.skipped is True
    assert again.snapshot_id == first.snapshot_id
    assert len(table.snapshots()) == n_snapshots
    assert table.scan().to_arrow().num_rows == 50


def test_quarantined_rows_land_in_quarantine_table(
    clean: pl.DataFrame, orders: Contract, settings: Settings, ns: str, table_of: Any
) -> None:
    result = _load(_out(clean, quarantined={(999_001,), (999_002,)}), orders, settings, ns, _sha())

    rows = table_of("orders_quarantine").scan().to_arrow().to_pylist()
    assert result.rows_quarantined == 2
    assert sorted(r["pk"] for r in rows) == ["[999001]", "[999002]"]
    assert all(r["reason"] for r in rows)


def test_flagged_cells_null_in_table_and_recorded_in_flags(
    clean: pl.DataFrame,
    orders: Contract,
    settings: Settings,
    ns: str,
    table_of: Any,
    pg: psycopg.Connection,
) -> None:
    key = int(clean["o_orderkey"][0])
    df = clean.with_columns(
        pl.when(pl.col("o_orderkey") == key)
        .then(None)
        .otherwise(pl.col("o_comment"))
        .alias("o_comment")
    )
    run_id = secrets.token_hex(16)
    _load(_out(df, flags={((key,), "o_comment")}), orders, settings, ns, _sha(), run_id)

    loaded = pl.from_arrow(table_of("orders").scan().to_arrow())
    assert loaded.filter(pl.col("o_orderkey") == key)["o_comment"].to_list() == [None]
    flags = _rows(pg, "SELECT pk, column_name FROM flags WHERE run_id = %s", run_id)
    assert flags == [(f"[{key}]", "o_comment")]


def test_lineage_rows_present(
    clean: pl.DataFrame, orders: Contract, settings: Settings, ns: str, pg: psycopg.Connection
) -> None:
    run_id = secrets.token_hex(16)
    sha = _sha()
    meta = {
        "plan_nodes": [
            {"node_id": "n1", "column_name": "o_comment", "tier": "small", "attempts": 2,
             "accepted": True},
        ],
        "transforms": [
            {"node_id": "n1", "attempt": 1, "code": "def transform(df): return df",
             "model": "small", "passed": False},
            {"node_id": "n1", "attempt": 2, "code": "def transform(df):\n    return df",
             "model": "small", "passed": True},
        ],
    }  # fmt: skip
    result = _load(_out(clean, cost_usd=0.0123, meta=meta), orders, settings, ns, sha, run_id)

    runs = _rows(
        pg,
        "SELECT file_sha256, table_name, status, cost_usd, started_at <= finished_at "
        "FROM runs WHERE run_id = %s",
        run_id,
    )
    assert runs == [(sha, "orders", "succeeded", pytest.approx(0.0123), True)]
    loads = _rows(
        pg,
        "SELECT file_sha256, contract_version, snapshot_id, rows_loaded, rows_quarantined "
        "FROM loads WHERE run_id = %s",
        run_id,
    )
    assert loads == [(sha, "v1", result.snapshot_id, 50, 0)]
    nodes = _rows(
        pg, "SELECT node_id, column_name, tier, attempts, accepted FROM plan_nodes "
        "WHERE run_id = %s", run_id
    )  # fmt: skip
    assert nodes == [("n1", "o_comment", "small", 2, True)]
    transforms = _rows(
        pg,
        "SELECT attempt, length(code_sha256), passed FROM transforms WHERE run_id = %s "
        "ORDER BY attempt",
        run_id,
    )
    assert transforms == [(1, 64, False), (2, 64, True)]


def test_crash_between_iceberg_commit_and_record_does_not_duplicate_on_retry(
    clean: pl.DataFrame,
    orders: Contract,
    settings: Settings,
    ns: str,
    table_of: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sha = _sha()
    real_record = iceberg._record_load

    def crash(*_a: object, **_k: object) -> None:
        raise RuntimeError("simulated crash after the Iceberg commit")

    monkeypatch.setattr(iceberg, "_record_load", crash)
    with pytest.raises(RuntimeError, match="simulated"):
        _load(_out(clean, quarantined={(999_001,)}), orders, settings, ns, sha)
    committed = table_of("orders").current_snapshot().snapshot_id
    monkeypatch.setattr(iceberg, "_record_load", real_record)

    retry = _load(_out(clean, quarantined={(999_001,)}), orders, settings, ns, sha)

    assert retry.skipped is False
    assert retry.recovered is True
    assert retry.snapshot_id == str(committed)
    assert table_of("orders").scan().to_arrow().num_rows == 50
    assert table_of("orders_quarantine").scan().to_arrow().num_rows == 1
    assert _load(_out(clean), orders, settings, ns, sha).skipped is True


def test_rollback_restores_previous_snapshot(
    clean: pl.DataFrame,
    orders: Contract,
    settings: Settings,
    ns: str,
    table_of: Any,
    pg: psycopg.Connection,
) -> None:
    first = _load(_out(clean.head(20)), orders, settings, ns, _sha())
    second_sha = _sha()
    second_run = secrets.token_hex(16)
    _load(_out(clean.tail(30)), orders, settings, ns, second_sha, second_run)
    assert table_of("orders").scan().to_arrow().num_rows == 50
    assert first.snapshot_id is not None

    rollback("orders", first.snapshot_id, settings=settings, namespace=ns)

    table = table_of("orders")
    assert str(table.current_snapshot().snapshot_id) == first.snapshot_id
    assert table.scan().to_arrow().num_rows == 20
    status = _rows(pg, "SELECT status FROM runs WHERE run_id = %s", second_run)
    assert status == [("rolled_back",)]
    # The rolled-back file is no longer recorded as loaded, so it can be loaded again.
    reload = _load(_out(clean.tail(30)), orders, settings, ns, second_sha)
    assert reload.skipped is False
    assert table_of("orders").scan().to_arrow().num_rows == 50


def test_rollback_unknown_snapshot_raises(
    clean: pl.DataFrame, orders: Contract, settings: Settings, ns: str
) -> None:
    _load(_out(clean), orders, settings, ns, _sha())
    with pytest.raises(iceberg.LoadError, match="snapshot"):
        rollback("orders", "1", settings=settings, namespace=ns)


def test_cli_run_loads_orders_file_idempotently(
    orders_df: pl.DataFrame, tmp_path: Path, settings: Settings
) -> None:
    path = tmp_path / f"orders-{secrets.token_hex(4)}.parquet"
    # A random sample, so the file hash (the idempotency key) is new on every test run.
    orders_df.sample(40, seed=secrets.randbelow(2**31)).write_parquet(path)
    runner = CliRunner()

    first = runner.invoke(cli.app, ["run", str(path), "--table", "orders"])
    second = runner.invoke(cli.app, ["run", str(path), "--table", "orders"])

    assert first.exit_code == 0, first.output
    assert '"type":"loaded"' in first.stdout
    assert second.exit_code == 0, second.output
    assert "already loaded" in second.stdout
    assert '"type":"loaded"' not in second.stdout


# --- fix round 1 (Ruling L1): the key is (iceberg_table, file_sha256, contract_version) ------


@pytest.fixture
def lineitem_clean(lineitem_df: pl.DataFrame) -> tuple[Contract, pl.DataFrame]:
    contract = load_contract("lineitem")
    schema = contract.polars_schema()
    return contract, lineitem_df.head(30).select(list(schema)).cast(schema)  # type: ignore[arg-type]


def test_same_file_into_another_table_is_not_skipped(
    clean: pl.DataFrame,
    orders: Contract,
    lineitem_clean: tuple[Contract, pl.DataFrame],
    settings: Settings,
    ns: str,
    table_of: Any,
) -> None:
    sha = _sha()
    li_contract, li_df = lineitem_clean
    first = _load(_out(clean), orders, settings, ns, sha)

    second = _load(_out(li_df), li_contract, settings, ns, sha)

    assert first.skipped is False
    assert second.skipped is False
    assert table_of("lineitem").scan().to_arrow().num_rows == 30
    assert _load(_out(li_df), li_contract, settings, ns, sha).skipped is True
    assert _load(_out(clean), orders, settings, ns, sha).skipped is True


def test_find_load_reports_recorded_key_per_table(
    clean: pl.DataFrame, orders: Contract, settings: Settings, ns: str
) -> None:
    sha = _sha()
    assert iceberg.find_load(orders, sha, settings=settings, namespace=ns) is None
    loaded = _load(_out(clean), orders, settings, ns, sha)

    found = iceberg.find_load(orders, sha, settings=settings, namespace=ns)

    assert found is not None and found.skipped is True
    assert found.snapshot_id == loaded.snapshot_id
    other = iceberg.find_load(load_contract("lineitem"), sha, settings=settings, namespace=ns)
    assert other is None


def test_cli_repeat_run_does_not_run_pipeline(
    orders_df: pl.DataFrame, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / f"orders-{secrets.token_hex(4)}.parquet"
    orders_df.sample(25, seed=secrets.randbelow(2**31)).write_parquet(path)
    runner = CliRunner()
    assert runner.invoke(cli.app, ["run", str(path), "--table", "orders"]).exit_code == 0

    def must_not_run(_note: object) -> object:
        pytest.fail("pipeline resolved for an already-loaded file")

    monkeypatch.setattr(cli, "resolve_run_fn", must_not_run)
    again = runner.invoke(cli.app, ["run", str(path), "--table", "orders"])
    assert again.exit_code == 0, again.output
    assert "already loaded" in again.stdout
