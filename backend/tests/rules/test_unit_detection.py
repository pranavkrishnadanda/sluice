"""Ruling H2a: the profiler flags an INR decimal column whose values look like whole paise."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import polars as pl
import pytest

from sluice.contracts.model import Contract, load_contract
from sluice.rules.fixers import apply_rules
from sluice.rules.profile import (
    UNIT_MIN_VALUES,
    magnitude,
    profile,
)


@pytest.fixture(scope="module")
def orders() -> Contract:
    return load_contract("orders")


@pytest.fixture(scope="module")
def lineitem() -> Contract:
    return load_contract("lineitem")


def _orders_frame(prices: list[str | None]) -> pl.DataFrame:
    n = len(prices)
    return pl.DataFrame(
        {
            "o_orderkey": [str(i + 1) for i in range(n)],
            "o_totalprice": pl.Series(prices, dtype=pl.String),
        }
    )


def _paise(n: int) -> list[str]:
    return [str(1_000_000 + 1_237 * i) for i in range(n)]  # whole numbers, 7 digits


def _indian(value: int) -> str:
    text = str(value)
    head, tail = text[:-3], text[-3:]
    groups = []
    while len(head) > 2:
        groups.insert(0, head[-2:])
        head = head[:-2]
    if head:
        groups.insert(0, head)
    return ",".join([*groups, tail]) if groups else tail


def test_profile_unit_issue_for_whole_number_paise(orders: Contract) -> None:
    prof = profile(_orders_frame(_paise(60)), orders)
    detail = prof.unit_issues["o_totalprice"]
    assert detail.startswith("values look like paise: 100% whole numbers, median ")
    assert detail.endswith("; expected INR with paise as decimals")


def test_profile_unit_issue_for_indian_grouped_paise(orders: Contract) -> None:
    grouped = [_indian(int(v)) for v in _paise(60)]
    assert grouped[0] == "10,00,000"
    prof = profile(_orders_frame(grouped), orders)
    assert "o_totalprice" in prof.unit_issues
    assert "median 1036" in prof.unit_issues["o_totalprice"]


def test_profile_unit_issue_counts_zero_fraction_as_whole(orders: Contract) -> None:
    prof = profile(_orders_frame([f"{v}.00" for v in _paise(60)]), orders)
    assert "o_totalprice" in prof.unit_issues


def test_profile_unit_issue_on_typed_decimal_paise(orders: Contract) -> None:
    df = pl.DataFrame(
        {
            "o_orderkey": list(range(1, 61)),
            "o_totalprice": pl.Series([Decimal(v) for v in _paise(60)], dtype=pl.Decimal(15, 2)),
        }
    )
    assert "o_totalprice" in profile(df, orders).unit_issues


def test_profile_no_unit_issue_for_genuine_inr_decimals(orders: Contract) -> None:
    prices = [f"{10_000 + 13 * i}.{(i * 7) % 100:02d}" for i in range(200)]
    assert not profile(_orders_frame(prices), orders).unit_issues


def test_profile_no_unit_issue_for_grouped_inr_with_paise_decimals(orders: Contract) -> None:
    prices = [f"{_indian(1_000_000 + 7 * i)}.{(i * 3) % 100 or 1:02d}" for i in range(200)]
    assert not profile(_orders_frame(prices), orders).unit_issues


def test_profile_no_unit_issue_below_the_minimum_parseable_count(orders: Contract) -> None:
    few = _paise(UNIT_MIN_VALUES - 1) + ["N/A"] * 40 + ["abc"] * 10
    assert not profile(_orders_frame(few), orders).unit_issues


def test_profile_unit_issue_at_exactly_98_percent_whole(orders: Contract) -> None:
    prices = _paise(98) + ["12345.67", "23456.78"]
    assert "o_totalprice" in profile(_orders_frame(prices), orders).unit_issues


def test_profile_no_unit_issue_at_97_percent_whole(orders: Contract) -> None:
    prices = _paise(97) + ["12345.67", "23456.78", "34567.89"]
    assert not profile(_orders_frame(prices), orders).unit_issues


def test_profile_unit_issue_ignores_destroyed_tokens(orders: Contract) -> None:
    prices = _paise(60) + ["-1"] * 5 + ["N/A", "", "###"]
    assert "o_totalprice" in profile(_orders_frame(prices), orders).unit_issues


def test_profile_no_unit_issue_on_a_decimal_column_without_unit(lineitem: Contract) -> None:
    df = pl.DataFrame(
        {
            "l_orderkey": [str(i) for i in range(100)],
            "l_linenumber": ["1"] * 100,
            "l_quantity": [f"{i % 50 + 1}.00" for i in range(100)],  # whole, but no unit
        }
    )
    assert not profile(df, lineitem).unit_issues


def test_profile_unit_issue_does_not_change_the_rules_output(orders: Contract) -> None:
    df = _orders_frame([f"{v}.00" for v in _paise(60)])
    prof = profile(df, orders)
    assert prof.unit_issues
    with_issue = apply_rules(df, orders, prof, {})
    without = apply_rules(df, orders, replace(prof, unit_issues={}), {})
    assert with_issue.df.equals(without.df)
    assert with_issue.flags == without.flags
    assert with_issue.actions == without.actions


def test_magnitude_stats_of_paise_and_rupees() -> None:
    stats = magnitude(pl.Series(["1,00,000", " 2,00,000 ", "300000", "N/A", None, "abc"]))
    assert stats is not None
    assert (stats.count, stats.whole_share, stats.median) == (3, 1.0, 200_000.0)
    rupees = magnitude(pl.Series(["1000.50", "2000.25", "3000.00"]))
    assert rupees is not None
    assert rupees.whole_share == pytest.approx(1 / 3)
    assert rupees.median == 2000.25
    assert rupees.p5 <= rupees.median <= rupees.p95
    assert magnitude(pl.Series(["N/A", None], dtype=pl.String)) is None


def test_magnitude_ignores_non_ascii_digits() -> None:
    stats = magnitude(pl.Series(["\u0967\u0968\u0969.00", "12.00"]))
    assert stats is not None and stats.count == 1
