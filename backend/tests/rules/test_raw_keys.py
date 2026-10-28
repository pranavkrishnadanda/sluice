"""Public helpers that let later stages align raw rows with the rules' clean primary keys."""

import polars as pl

from sluice.contracts.model import load_contract
from sluice.rules.fixers import apply_rules, cell_texts, raw_keys
from sluice.rules.profile import profile


def test_raw_keys_parses_keys_exactly_as_the_rules_do() -> None:
    orders = load_contract("orders")
    raw = pl.DataFrame({"o_orderkey": ["1", " 2 ", "x", None, "N/A"]})

    assert raw_keys(raw, orders) == [(1,), (2,), None, None, None]


def test_raw_keys_matches_pk_columns_by_normalised_name() -> None:
    orders = load_contract("orders")
    raw = pl.DataFrame({" O_OrderKey ": ["7"]})

    assert raw_keys(raw, orders) == [(7,)]


def test_raw_keys_composite_key_is_none_when_any_part_fails() -> None:
    lineitem = load_contract("lineitem")
    raw = pl.DataFrame({"l_orderkey": ["1", "1"], "l_linenumber": ["3", "?"]})

    assert raw_keys(raw, lineitem) == [(1, 3), None]


def test_raw_keys_agree_with_rule_flag_keys() -> None:
    orders = load_contract("orders")
    raw = pl.DataFrame({"o_orderkey": ["5", "6"], "o_custkey": ["oops", "9"]})
    result = apply_rules(raw, orders, profile(raw, orders), {})

    keys = raw_keys(raw, orders)

    assert (keys[0], "o_custkey") in result.flags
    assert (keys[1], "o_custkey") not in result.flags


def test_raw_keys_missing_pk_column_gives_all_none() -> None:
    orders = load_contract("orders")
    raw = pl.DataFrame({"o_custkey": ["1", "2"]})

    assert raw_keys(raw, orders) == [None, None]


def test_cell_texts_renders_typed_cells_and_rejects_unsupported() -> None:
    assert cell_texts(pl.Series([1, None])) == ["1", None]
    assert cell_texts(pl.Series(["a"])) == ["a"]
    assert cell_texts(pl.Series([1.5])) is None
