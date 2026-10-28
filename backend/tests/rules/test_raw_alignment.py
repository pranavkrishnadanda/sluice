"""Public helpers that let later stages align raw rows with the rules' clean primary keys."""

import polars as pl
import pytest

from sluice.contracts.model import load_contract
from sluice.rules.fixers import UnkeyableInputError, apply_rules, cell_texts, kept_raw_rows
from sluice.rules.profile import profile


def kept(raw: pl.DataFrame, table: str = "orders") -> dict:
    contract = load_contract(table)
    return kept_raw_rows(raw, contract, profile(raw, contract))


def test_kept_raw_rows_parses_keys_exactly_as_the_rules_do() -> None:
    raw = pl.DataFrame({"o_orderkey": ["1", " 2 ", "x", None, "N/A"]})

    assert kept(raw) == {(1,): 0, (2,): 1}


def test_kept_raw_rows_matches_pk_columns_by_normalised_name() -> None:
    raw = pl.DataFrame({" O_OrderKey ": ["7"]})

    assert kept(raw) == {(7,): 0}


def test_kept_raw_rows_composite_key_skips_rows_with_a_failed_part() -> None:
    raw = pl.DataFrame({"l_orderkey": ["1", "1"], "l_linenumber": ["3", "?"]})

    assert kept(raw, "lineitem") == {(1, 3): 0}


def test_kept_raw_rows_picks_the_duplicate_the_rules_keep() -> None:
    # Row 1 carries more flags, so the rules keep it over row 0.
    orders = load_contract("orders")
    raw = pl.DataFrame({"o_orderkey": ["5", "5"], "o_custkey": ["9", "oops"]})
    result = apply_rules(raw, orders, profile(raw, orders), {})

    assert kept(raw) == {(5,): 1}
    assert result.df["o_custkey"].to_list() == [None]


def test_kept_raw_rows_ties_and_exact_duplicates_keep_the_earliest_row() -> None:
    raw = pl.DataFrame({"o_orderkey": ["5", "5", "5"], "o_custkey": ["a", "b", "c"]})

    assert kept(raw) == {(5,): 0}


def test_kept_raw_rows_missing_pk_column_raises_like_the_rules() -> None:
    raw = pl.DataFrame({"o_custkey": ["1", "2"]})

    with pytest.raises(UnkeyableInputError):
        kept(raw)


def test_cell_texts_renders_typed_cells_and_rejects_unsupported() -> None:
    assert cell_texts(pl.Series([1, None])) == ["1", None]
    assert cell_texts(pl.Series(["a"])) == ["a"]
    assert cell_texts(pl.Series([1.5])) is None
