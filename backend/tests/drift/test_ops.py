import json
import re
from dataclasses import asdict
from datetime import UTC, date, datetime
from decimal import Decimal

import numpy as np
import polars as pl
import pytest

from sluice.contracts.model import load_contract
from sluice.drift.ops import OPS, DriftError, format_indian
from sluice.drift.state import DriftState, ManifestEntry

SEED = 7


@pytest.fixture(scope="session")
def orders20(orders_df: pl.DataFrame) -> pl.DataFrame:
    return orders_df.head(20)


def _orders_state(df: pl.DataFrame) -> DriftState:
    return DriftState.start(load_contract("orders"), df)


def _rng(seed: int = SEED) -> np.random.Generator:
    return np.random.default_rng(seed)


def _set_first(df: pl.DataFrame, col: str, values: list[object]) -> pl.DataFrame:
    """Copy of ``df`` with the first ``len(values)`` cells of ``col`` replaced."""
    dtype = df.schema[col]
    new = pl.Series(col, values + df[col].to_list()[len(values) :], dtype=dtype)
    return df.with_columns(new)


def _pk_of(state: DriftState, row: int) -> tuple:
    return tuple(state.expected.row(row, named=True)[k] for k in state.contract.primary_key)


# --- state ------------------------------------------------------------------------------------


def test_start_copies_clean_frame_and_identity_names(orders20: pl.DataFrame) -> None:
    state = _orders_state(orders20)
    assert state.df.equals(orders20)
    assert state.expected.equals(orders20)
    assert state.current_name == {c: c for c in orders20.columns}
    assert (state.flags, state.quarantined, state.entries) == (set(), set(), [])


def test_start_rejects_schema_mismatch(orders20: pl.DataFrame) -> None:
    with pytest.raises(ValueError, match="schema"):
        _orders_state(orders20.drop("o_clerk"))


def test_start_rejects_duplicate_pk(orders20: pl.DataFrame) -> None:
    with pytest.raises(ValueError, match="primary key"):
        _orders_state(pl.concat([orders20, orders20.head(1)]))


def test_ops_registry_covers_every_op() -> None:
    assert set(OPS) == {
        "rename",
        "indian_number_string",
        "unit_paise",
        "tz_ist",
        "date_format_mix",
        "whitespace",
        "case_noise",
        "destroy",
        "duplicate_rows",
        "fk_break",
        "drop_column",
        "add_column",
        "unicode_text",
    }


# --- brief tests ------------------------------------------------------------------------------


def test_rename_tracks_current_name(orders20: pl.DataFrame) -> None:
    state = _orders_state(orders20)
    OPS["rename"](state, _rng(), column="o_totalprice", new_name="amt")
    assert state.current_name["o_totalprice"] == "amt"
    assert "amt" in state.df.columns and "o_totalprice" not in state.df.columns
    assert state.entries == [
        ManifestEntry(
            op="rename",
            params={"column": "o_totalprice", "new_name": "amt"},
            columns=["o_totalprice"],
            rows_affected=20,
        )
    ]
    assert state.expected.equals(orders20)


def test_indian_number_string(orders20: pl.DataFrame) -> None:
    df = _set_first(orders20, "o_totalprice", [Decimal("124500.50"), Decimal("999.00")])
    state = _orders_state(df)
    OPS["indian_number_string"](state, _rng(), column="o_totalprice")
    col = state.df["o_totalprice"]
    assert col.dtype == pl.String
    assert col.to_list()[:2] == ["1,24,500.50", "999.00"]
    assert state.expected.equals(df)
    assert state.entries[-1].rows_affected == 20


@pytest.mark.parametrize(
    ("value", "text"),
    [
        (Decimal("124500.50"), "1,24,500.50"),
        (Decimal("999.00"), "999.00"),
        (Decimal("1000.00"), "1,000.00"),
        (Decimal("10000000.00"), "1,00,00,000.00"),
        (Decimal("0.05"), "0.05"),
        (Decimal("-124500.50"), "-1,24,500.50"),
        (1234567, "12,34,567"),
    ],
)
def test_format_indian_grouping(value: Decimal | int, text: str) -> None:
    assert format_indian(value) == text


def test_paise_roundtrip_exact(orders20: pl.DataFrame) -> None:
    df = _set_first(orders20, "o_totalprice", [Decimal("466001.28")])
    state = _orders_state(df)
    OPS["unit_paise"](state, _rng(), column="o_totalprice", hinted=False)
    col = state.df["o_totalprice"]
    assert col.dtype == pl.Int64
    assert col[0] == 46600128
    assert Decimal(col[0]) / 100 == Decimal("466001.28")
    assert [Decimal(v) / 100 for v in col.to_list()] == df["o_totalprice"].to_list()


def test_paise_hinted_renames_current_column(orders20: pl.DataFrame) -> None:
    state = _orders_state(orders20)
    OPS["rename"](state, _rng(), column="o_totalprice", new_name="amt")
    OPS["unit_paise"](state, _rng(), column="o_totalprice", hinted=True)
    assert state.current_name["o_totalprice"] == "amt_paise"
    assert state.df.schema["amt_paise"] == pl.Int64


def test_tz_ist_offset(orders20: pl.DataFrame) -> None:
    df = _set_first(orders20, "o_ordertime", [datetime(2026, 10, 3, 18, 45, tzinfo=UTC)])
    state = _orders_state(df)
    OPS["tz_ist"](state, _rng(), column="o_ordertime")
    col = state.df["o_ordertime"]
    assert col[0] == "2026-10-04 00:15:00+05:30"
    assert all(re.fullmatch(r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\+05:30", v) for v in col)
    assert state.expected.equals(df)


def test_destroy_sets_flags(orders20: pl.DataFrame) -> None:
    state = _orders_state(orders20)
    OPS["destroy"](state, _rng(), column="o_totalprice", frac=0.25, token="N/A")
    entry = state.entries[-1]
    assert entry.rows_affected == 5
    assert len(state.flags) == entry.rows_affected
    flagged = {pk for pk, col in state.flags if col == "o_totalprice"}
    assert len(flagged) == 5
    exp = state.expected
    for i in range(exp.height):
        pk = _pk_of(state, i)
        cell = exp["o_totalprice"][i]
        assert (cell is None) == (pk in flagged)
    df_vals = dict(zip(state.df["o_orderkey"], state.df["o_totalprice"], strict=True))
    assert all(df_vals[pk[0]] == "N/A" for pk in flagged)
    assert exp.schema == orders20.schema


def test_destroy_int_sentinel_keeps_int_dtype(orders20: pl.DataFrame) -> None:
    state = _orders_state(orders20)
    OPS["destroy"](state, _rng(), column="o_custkey", frac=0.1, token="-1")
    assert state.df.schema["o_custkey"] == pl.Int64
    assert (state.df["o_custkey"] == -1).sum() == 2


def test_destroy_rejects_unknown_token(orders20: pl.DataFrame) -> None:
    with pytest.raises(DriftError, match="token"):
        OPS["destroy"](_orders_state(orders20), _rng(), column="o_clerk", frac=0.1, token="?")


def test_destroy_after_rename_uses_clean_pk(orders20: pl.DataFrame) -> None:
    state = _orders_state(orders20)
    OPS["rename"](state, _rng(), column="o_orderkey", new_name="order_id")
    OPS["tz_ist"](state, _rng(), column="o_ordertime")
    OPS["destroy"](state, _rng(), column="o_clerk", frac=0.5, token="###")
    keys = set(orders20["o_orderkey"].to_list())
    assert len(state.flags) == 10
    assert all(pk[0] in keys and col == "o_clerk" for pk, col in state.flags)


def test_fk_break_quarantines(lineitem_df: pl.DataFrame) -> None:
    clean = lineitem_df.head(50)
    state = DriftState.start(load_contract("lineitem"), clean)
    OPS["fk_break"](state, _rng(), column="l_orderkey", frac=0.2)
    assert len(state.quarantined) == 10 == state.entries[-1].rows_affected
    exp_pks = set(state.expected.select("l_orderkey", "l_linenumber").rows())
    assert state.quarantined.isdisjoint(exp_pks)
    assert len(exp_pks) == 40
    assert state.df.height == 50
    broken = state.df.filter(pl.col("l_orderkey") < 0)
    assert broken.height == 10
    parents = set(clean["l_orderkey"].to_list())
    assert parents.isdisjoint(broken["l_orderkey"].to_list())


def test_fk_break_rejects_non_fk_column(orders20: pl.DataFrame) -> None:
    with pytest.raises(DriftError, match="foreign key"):
        OPS["fk_break"](_orders_state(orders20), _rng(), column="o_custkey", frac=0.1)


def test_fk_break_drops_flags_of_quarantined_rows(lineitem_df: pl.DataFrame) -> None:
    state = DriftState.start(load_contract("lineitem"), lineitem_df.head(20))
    OPS["drop_column"](state, _rng(), column="l_comment")
    OPS["fk_break"](state, _rng(), column="l_orderkey", frac=0.5)
    assert {pk for pk, _ in state.flags}.isdisjoint(state.quarantined)
    assert len(state.flags) == 10


def test_drop_column_flags_all(orders20: pl.DataFrame) -> None:
    state = _orders_state(orders20)
    OPS["drop_column"](state, _rng(), column="o_clerk")
    assert state.flags == {((k,), "o_clerk") for k in orders20["o_orderkey"].to_list()}
    assert "o_clerk" not in state.df.columns
    assert "o_clerk" not in state.current_name
    assert state.expected.schema == orders20.schema
    assert state.expected["o_clerk"].null_count() == 20
    with pytest.raises(DriftError, match="dropped"):
        OPS["whitespace"](state, _rng(), column="o_clerk", frac=0.5)


def test_drop_column_rejects_primary_key(orders20: pl.DataFrame) -> None:
    with pytest.raises(DriftError, match="primary key"):
        OPS["drop_column"](_orders_state(orders20), _rng(), column="o_orderkey")


def test_unicode_text_changes_expected(orders20: pl.DataFrame) -> None:
    state = _orders_state(orders20)
    OPS["whitespace"](state, _rng(1), column="o_comment", frac=1.0)
    OPS["unicode_text"](state, _rng(), column="o_comment", frac=0.5)
    assert state.entries[-1].rows_affected == 10
    exp_vals = state.expected["o_comment"].to_list()
    # TPC-H comments can carry their own trailing spaces, so compare modulo padding here.
    assert [v.strip() for v in state.df["o_comment"]] == [v.strip() for v in exp_vals]
    changed = [
        i
        for i, (old, new) in enumerate(zip(orders20["o_comment"], exp_vals, strict=True))
        if old != new
    ]
    assert len(changed) == 10
    for i in changed:
        old, new = orders20["o_comment"][i], exp_vals[i]
        diffs = [(a, b) for a, b in zip(old, new, strict=True) if a != b]
        assert len(diffs) == 1
        assert diffs[0][0].isascii() and diffs[0][0].isalpha()
        assert diffs[0][1] in "éñü"
    assert state.flags == set()


def test_unicode_text_df_equals_expected_exactly(orders20: pl.DataFrame) -> None:
    state = _orders_state(orders20)
    OPS["unicode_text"](state, _rng(), column="o_comment", frac=0.5)
    assert state.df["o_comment"].equals(state.expected["o_comment"])
    assert not state.expected["o_comment"].equals(orders20["o_comment"])


def test_ops_deterministic(orders20: pl.DataFrame) -> None:
    def run(seed: int) -> DriftState:
        state = _orders_state(orders20)
        rng = _rng(seed)
        OPS["rename"](state, rng, column="o_totalprice", new_name="amt")
        OPS["destroy"](state, rng, column="o_totalprice", frac=0.2, token="")
        OPS["date_format_mix"](
            state, rng, column="o_orderdate", frac=0.5, formats=["%d/%m/%Y", "%d %b %Y"]
        )
        OPS["whitespace"](state, rng, column="o_clerk", frac=0.3)
        OPS["case_noise"](state, rng, column="o_orderpriority", frac=0.4)
        OPS["unicode_text"](state, rng, column="o_comment", frac=0.3)
        OPS["duplicate_rows"](state, rng, frac=0.1)
        OPS["add_column"](state, rng, name="source_tag")
        return state

    a, b, c = run(SEED), run(SEED), run(SEED + 1)
    assert a.df.equals(b.df) and a.expected.equals(b.expected)
    assert a.entries == b.entries and a.flags == b.flags
    assert not a.df.equals(c.df)
    assert len(a.entries) == 8


# --- remaining ops ----------------------------------------------------------------------------


def test_date_format_mix(orders20: pl.DataFrame) -> None:
    df = _set_first(orders20, "o_orderdate", [date(2026, 1, 3)])
    state = _orders_state(df)
    OPS["date_format_mix"](state, _rng(), column="o_orderdate", frac=1.0, formats=["%d %b %Y"])
    assert state.df["o_orderdate"][0] == "03 Jan 2026"
    assert state.entries[-1].rows_affected == 20

    state = _orders_state(df)
    OPS["date_format_mix"](state, _rng(), column="o_orderdate", frac=0.5, formats=["%d/%m/%Y"])
    vals = state.df["o_orderdate"].to_list()
    assert sum(bool(re.fullmatch(r"\d\d/\d\d/\d{4}", v)) for v in vals) == 10
    assert sum(bool(re.fullmatch(r"\d{4}-\d\d-\d\d", v)) for v in vals) == 10


def test_date_format_mix_rejects_unknown_format(orders20: pl.DataFrame) -> None:
    with pytest.raises(DriftError, match="format"):
        OPS["date_format_mix"](
            _orders_state(orders20), _rng(), column="o_orderdate", frac=0.5, formats=["%Y"]
        )


def test_whitespace_pads_both_sides(orders20: pl.DataFrame) -> None:
    state = _orders_state(orders20)
    OPS["whitespace"](state, _rng(), column="o_clerk", frac=0.5)
    vals = state.df["o_clerk"].to_list()
    padded = [v for v in vals if v != v.strip()]
    assert len(padded) == 10 == state.entries[-1].rows_affected
    assert all(re.fullmatch(r" {1,3}\S+ {1,3}", v) for v in padded)
    assert [v.strip() for v in vals] == orders20["o_clerk"].to_list()


def test_case_noise_enum_only(orders20: pl.DataFrame) -> None:
    state = _orders_state(orders20)
    OPS["case_noise"](state, _rng(), column="o_orderpriority", frac=0.5)
    vals = state.df["o_orderpriority"].to_list()
    assert sum(v != o for v, o in zip(vals, orders20["o_orderpriority"], strict=True)) == 10
    assert [v.upper() for v in vals] == orders20["o_orderpriority"].to_list()
    with pytest.raises(DriftError, match="enum"):
        OPS["case_noise"](state, _rng(), column="o_clerk", frac=0.5)


def test_duplicate_rows_appends_exact_copies(orders20: pl.DataFrame) -> None:
    state = _orders_state(orders20)
    OPS["duplicate_rows"](state, _rng(), frac=0.25)
    assert state.df.height == 25
    assert state.df.unique().height == 20
    assert state.expected.equals(orders20)
    assert state.entries[-1] == ManifestEntry("duplicate_rows", {"frac": 0.25}, [], 5)


def test_duplicate_then_destroy_hits_every_copy(orders20: pl.DataFrame) -> None:
    state = _orders_state(orders20)
    OPS["duplicate_rows"](state, _rng(), frac=1.0)
    OPS["destroy"](state, _rng(), column="o_clerk", frac=0.5, token="N/A")
    flagged = {pk[0] for pk, _ in state.flags}
    destroyed = set(state.df.filter(pl.col("o_clerk") == "N/A")["o_orderkey"].to_list())
    assert destroyed == flagged and len(flagged) == 10
    assert state.entries[-1].rows_affected == 20


def test_add_column_random_words(orders20: pl.DataFrame) -> None:
    state = _orders_state(orders20)
    OPS["add_column"](state, _rng(), name="note")
    col = state.df["note"]
    assert col.dtype == pl.String and col.null_count() == 0
    assert all(re.fullmatch(r"[a-z]+( [a-z]+)*", v) for v in col)
    assert state.expected.equals(orders20)
    with pytest.raises(DriftError, match="exists"):
        OPS["add_column"](state, _rng(), name="note")


def test_unknown_column_raises(orders20: pl.DataFrame) -> None:
    with pytest.raises(DriftError, match="no column"):
        OPS["whitespace"](_orders_state(orders20), _rng(), column="nope", frac=0.5)


def test_frac_out_of_range_raises(orders20: pl.DataFrame) -> None:
    with pytest.raises(DriftError, match="frac"):
        OPS["whitespace"](_orders_state(orders20), _rng(), column="o_clerk", frac=1.5)


def test_wrong_current_dtype_raises(orders20: pl.DataFrame) -> None:
    state = _orders_state(orders20)
    OPS["indian_number_string"](state, _rng(), column="o_totalprice")
    with pytest.raises(DriftError, match="dtype"):
        OPS["unit_paise"](state, _rng(), column="o_totalprice", hinted=False)


def test_manifest_and_truth_survive_json_roundtrip(lineitem_df: pl.DataFrame) -> None:
    state = DriftState.start(load_contract("lineitem"), lineitem_df.head(30))
    rng = _rng()
    OPS["destroy"](state, rng, column="l_extendedprice", frac=0.2, token="-1")
    OPS["fk_break"](state, rng, column="l_orderkey", frac=0.1)
    OPS["date_format_mix"](state, rng, column="l_shipdate", frac=0.5, formats=["%d/%m/%Y"])
    blob = json.dumps(
        {
            "entries": [asdict(e) for e in state.entries],
            "flags": sorted([list(pk), col] for pk, col in state.flags),
            "quarantined": sorted(list(pk) for pk in state.quarantined),
        }
    )
    back = json.loads(blob)
    assert [ManifestEntry(**e) for e in back["entries"]] == state.entries
    assert {(tuple(pk), col) for pk, col in back["flags"]} == state.flags
    assert {tuple(pk) for pk in back["quarantined"]} == state.quarantined
    for pk, _ in state.flags:
        assert all(type(v) is int for v in pk)
