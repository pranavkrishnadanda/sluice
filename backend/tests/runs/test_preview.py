"""The before/after preview: built from the raw file and the system output, paged for the API."""

from __future__ import annotations

import io
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import polars as pl
import pytest

from sluice.contracts.model import Contract, load_contract
from sluice.evals.types import CaseInput, SystemOutput, pk_text
from sluice.runs.preview import PREVIEW_MAX_LIMIT, build_preview, page_preview

ORDERS = load_contract("orders")
LINEITEM = load_contract("lineitem")


def _output(contract: Contract, rows: list[dict[str, Any]], **kw: Any) -> SystemOutput:
    schema = contract.polars_schema()
    full = [{c: row.get(c) for c in schema} for row in rows]
    base: dict[str, Any] = {
        "df": pl.DataFrame(full, schema=schema, orient="row")
        if full
        else pl.DataFrame(schema=schema),
        "flags": set(),
        "quarantined": set(),
        "file_quarantined": False,
        "cost_usd": 0.0,
        "latency_s": 0.0,
        "meta": {},
    }
    base.update(kw)
    return SystemOutput(**base)


@pytest.fixture
def orders_preview() -> pl.DataFrame:
    raw = pl.DataFrame(
        {
            "o_orderkey": ["1", "2", "3", "4", "5"],
            "amt": ["1,24,500.50", "N/A", "10.00", None, "7"],
            "o_orderdate": ["27/10/2026", "01/01/2026", "02/01/2026", "03/01/2026", "x"],
            "o_ordertime": ["2026-10-27 10:14:03+05:30", None, None, None, None],
        }
    )
    ts = datetime(2026, 10, 27, 4, 44, 3, tzinfo=UTC)
    out = _output(
        ORDERS,
        [
            {
                "o_orderkey": 1,
                "o_totalprice": Decimal("124500.50"),
                "o_orderdate": date(2026, 10, 27),
                "o_ordertime": ts,
            },
            {"o_orderkey": 2, "o_totalprice": None, "o_orderdate": date(2026, 1, 1)},
            {"o_orderkey": 3, "o_totalprice": Decimal("10.00"), "o_orderdate": date(2026, 1, 2)},
            {"o_orderkey": 4, "o_orderdate": date(2026, 1, 3)},
            {"o_orderkey": 5, "o_totalprice": Decimal("7.00")},
        ],  # fmt: skip
        flags={((2,), "o_totalprice"), ((5,), "o_orderdate")},
        meta={
            "mapping": dict.fromkeys(ORDERS.polars_schema())
            | {
                "o_orderkey": "o_orderkey",
                "o_totalprice": "amt",
                "o_orderdate": "o_orderdate",
                "o_ordertime": "o_ordertime",
            }
        },
    )
    return build_preview(raw, out, ORDERS)


def _row(preview: pl.DataFrame, pk: str) -> dict[str, Any]:
    (row,) = preview.filter(pl.col("pk") == pk).to_dicts()
    return row


def test_build_preview_renamed_column_shows_raw_before_and_canonical_after(
    orders_preview: pl.DataFrame,
) -> None:
    cell = _row(orders_preview, "1")["cells"]["o_totalprice"]
    assert cell == {"before": "1,24,500.50", "after": "124500.50", "flagged": False}


def test_build_preview_destroyed_cell_is_null_after_and_flagged(
    orders_preview: pl.DataFrame,
) -> None:
    cell = _row(orders_preview, "2")["cells"]["o_totalprice"]
    assert cell == {"before": "N/A", "after": None, "flagged": True}


def test_build_preview_renders_dates_and_utc_timestamps(orders_preview: pl.DataFrame) -> None:
    cells = _row(orders_preview, "1")["cells"]
    assert cells["o_orderdate"]["after"] == "2026-10-27"
    assert cells["o_ordertime"]["before"] == "2026-10-27 10:14:03+05:30"
    assert cells["o_ordertime"]["after"] == "2026-10-27T04:44:03Z"
    assert cells["o_orderkey"] == {"before": "1", "after": "1", "flagged": False}


def test_build_preview_unmapped_column_has_null_before(orders_preview: pl.DataFrame) -> None:
    cell = _row(orders_preview, "3")["cells"]["o_custkey"]
    assert cell == {"before": None, "after": None, "flagged": False}


def test_build_preview_one_row_per_raw_row_in_input_order(orders_preview: pl.DataFrame) -> None:
    assert orders_preview["pk"].to_list() == ["1", "2", "3", "4", "5"]
    assert not orders_preview["quarantined"].any()


def test_build_preview_round_trips_through_parquet(orders_preview: pl.DataFrame) -> None:
    buf = io.BytesIO()
    orders_preview.write_parquet(buf)
    buf.seek(0)
    assert pl.read_parquet(buf).equals(orders_preview)


@pytest.fixture
def lineitem_preview() -> pl.DataFrame:
    raw = pl.DataFrame(
        {
            "l_orderkey": ["1", "1", "999"],
            "l_linenumber": ["3", "4", "1"],
            "l_quantity": ["17", "36", "8"],
        }
    )
    reason = "foreign_key: l_orderkey not in orders.o_orderkey"
    out = _output(
        LINEITEM,
        [
            {"l_orderkey": 1, "l_linenumber": 3, "l_quantity": Decimal("17.00")},
            {"l_orderkey": 1, "l_linenumber": 4, "l_quantity": Decimal("36.00")},
        ],
        quarantined={(999, 1)},
        meta={"quarantine_reasons": {pk_text((999, 1)): reason}},  # no "mapping": rules-only
    )
    return build_preview(raw, out, LINEITEM)


def test_build_preview_composite_pk_joins_parts_in_key_order(
    lineitem_preview: pl.DataFrame,
) -> None:
    assert lineitem_preview["pk"].to_list() == ["1|3", "1|4", "999|1"]
    assert _row(lineitem_preview, "1|3")["cells"]["l_quantity"]["after"] == "17.00"


def test_build_preview_fk_quarantined_row_carries_its_reason(
    lineitem_preview: pl.DataFrame,
) -> None:
    row = _row(lineitem_preview, "999|1")
    assert row["quarantined"] is True
    assert row["reason"] == "foreign_key: l_orderkey not in orders.o_orderkey"
    assert row["cells"]["l_quantity"] == {"before": "8", "after": None, "flagged": False}
    kept = _row(lineitem_preview, "1|3")
    assert kept["quarantined"] is False and kept["reason"] is None


def test_build_preview_whole_file_quarantine_holds_every_row_back() -> None:
    raw = pl.DataFrame({"o_orderkey": ["1", "2"], "amt": ["3", "4"]})
    out = _output(
        ORDERS, [], file_quarantined=True, meta={"file_quarantine_reason": "node n1 failed"}
    )
    preview = build_preview(raw, out, ORDERS)
    assert preview["quarantined"].to_list() == [True, True]
    assert preview["reason"].to_list() == ["node n1 failed", "node n1 failed"]


def test_page_preview_first_page_has_next_offset(orders_preview: pl.DataFrame) -> None:
    page = page_preview(orders_preview, ORDERS, offset=0, limit=2)
    assert [r.pk for r in page.rows] == ["1", "2"]
    assert page.next_offset == 2
    assert page.total_rows == 5
    assert page.columns == [c.name for c in ORDERS.columns]
    assert page.rows[1].cells["o_totalprice"].flagged is True


def test_page_preview_last_page_has_null_next_offset(orders_preview: pl.DataFrame) -> None:
    page = page_preview(orders_preview, ORDERS, offset=4, limit=2)
    assert [r.pk for r in page.rows] == ["5"]
    assert page.next_offset is None
    past = page_preview(orders_preview, ORDERS, offset=10, limit=2)
    assert past.rows == [] and past.next_offset is None


def test_page_preview_limit_is_capped(orders_preview: pl.DataFrame) -> None:
    big = pl.concat([orders_preview] * 300)  # 1,500 rows
    page = page_preview(big, ORDERS, offset=0, limit=5000)
    assert PREVIEW_MAX_LIMIT == 1_000
    assert len(page.rows) == 1_000
    assert page.next_offset == 1_000


def test_page_preview_rejects_negative_offset_and_non_positive_limit(
    orders_preview: pl.DataFrame,
) -> None:
    with pytest.raises(ValueError):
        page_preview(orders_preview, ORDERS, offset=-1, limit=2)
    with pytest.raises(ValueError):
        page_preview(orders_preview, ORDERS, offset=0, limit=0)


def test_page_preview_serialises_to_the_contract_shape(orders_preview: pl.DataFrame) -> None:
    body = page_preview(orders_preview, ORDERS, offset=0, limit=1).model_dump()
    assert set(body) == {"columns", "rows", "totalRows", "nextOffset"}
    assert set(body["rows"][0]) == {"pk", "quarantined", "reason", "cells"}


def test_build_preview_unparseable_key_row_is_held_back_with_its_raw_key() -> None:
    raw = pl.DataFrame({"o_orderkey": ["1", "x"]})
    out = _output(ORDERS, [{"o_orderkey": 1}])
    preview = build_preview(raw, out, ORDERS)
    assert preview["pk"].to_list() == ["1", "x"]
    assert preview["quarantined"].to_list() == [False, True]
    assert preview["reason"][1] == "primary key could not be parsed"


def test_build_preview_unkeyable_file_still_has_one_row_per_raw_row() -> None:
    raw = pl.DataFrame({"amt": ["1", "2"]})
    out = _output(ORDERS, [], file_quarantined=True)
    preview = build_preview(raw, out, ORDERS)
    assert preview.height == 2
    assert preview["reason"].to_list() == ["file quarantined", "file quarantined"]


def test_build_preview_of_a_real_agent_run_traces_renamed_columns(tmp_path: Path) -> None:
    from pipeline_fakes import STRIP_COMMAS, FakeSandbox, Script, node
    from pydantic_ai.models.function import FunctionModel

    from sluice.io import read_input
    from sluice.llm.client import LLM
    from sluice.systems.agent import AgentSystem

    path = tmp_path / "orders.csv"
    pl.DataFrame(
        {
            "o_orderkey": ["1", "2"],
            "amt": ["1,24,500.50", "N/A"],
            "o_custkey": ["7", "8"],
            "o_orderstatus": ["F", "O"],
            "o_orderdate": ["27/10/2026", "28/10/2026"],
        }
    ).write_csv(path)
    script = Script(
        mapping={"mappings": [{"source": "amt", "target": "o_totalprice", "reason": "price"}]},
        plan={"nodes": [node("n1", "o_totalprice")]},
        workers={"o_totalprice": [STRIP_COMMAS]},
    )
    system = AgentSystem(
        llm_factory=lambda: LLM("http://t", "k", model_override=FunctionModel(script.respond)),
        sandbox=FakeSandbox(),
    )
    out = system.run(CaseInput(path, ORDERS, {}))
    preview = build_preview(read_input(path), out, ORDERS)
    first = _row(preview, "1")["cells"]
    assert first["o_totalprice"] == {
        "before": "1,24,500.50",
        "after": "124500.50",
        "flagged": False,
    }
    assert first["o_orderdate"] == {"before": "27/10/2026", "after": "2026-10-27", "flagged": False}
    second = _row(preview, "2")["cells"]["o_totalprice"]
    assert second == {"before": "N/A", "after": None, "flagged": True}
