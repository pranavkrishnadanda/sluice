"""Column profiling and deterministic input-to-contract column matching.

Matching is deliberately literal: an input column matches a contract column when its name is
identical, or identical after normalisation (strip, lower-case, ``-``/space -> ``_``). A name the
reader produced by flattening one level of nested JSON (``details.o_clerk``: exactly one dot,
non-empty container and leaf) is also tried by its leaf key (``o_clerk``) under the same rule:
that undoes the reader's own flattening. Deeper paths (``a.b.o_clerk``) never match.
There is no fuzzy or semantic matching (``orderdate`` never matches ``o_orderdate``); that is
the agent's job.

Ties: an exact name match always wins. Otherwise, if two or more input columns normalise to the
same contract column, none of them is matched (ambiguity is never resolved by guessing).

Unit check (Ruling H2a): for a matched contract column with ``unit: INR`` and dtype ``decimal``
the profile also reports, in ``unit_issues``, values that look like whole paise (the drift op
``unit_paise``: rupees x 100 with no fractional part). It is a report only: the rules never act
on it, so :func:`sluice.rules.fixers.apply_rules` output does not depend on it.

Complexity: O(rows x columns) for the null/distinct statistics, O(columns) for matching, plus one
vectorised O(rows) pass (and an O(rows log rows) median/quantile) per INR column.
"""

import re
from dataclasses import dataclass, field
from typing import Final

import polars as pl

from sluice.contracts.model import ColumnSpec, Contract

SAMPLE_SIZE = 5

SENTINEL_TOKENS: frozenset[str] = frozenset({"N/A", "", "-1", "###"})
"""Raw texts whose information is gone (compared after stripping). Re-exported by the rules."""

UNIT_CHECKED: Final = "INR"
"""The contract unit whose columns are checked for paise."""
UNIT_MIN_VALUES: Final = 50
"""Fewest parseable values the paise check needs before it says anything."""
UNIT_WHOLE_SHARE: Final = 0.98
"""Share of whole-number values at or above which an INR column looks like paise."""

_NUMBER_NOISE = r"[,\s]"  # Indian grouping commas and whitespace, removed before parsing
_NUMBER = r"^-?[0-9]+(\.[0-9]+)?$"  # ASCII digits only, like the rules (no Devanagari digits)
_WHOLE = r"^-?[0-9]+(\.0+)?$"
_SEPARATORS = re.compile(r"[-\s]+")
_NEST_SEP = "."


@dataclass(frozen=True, slots=True)
class ColumnProfile:
    """Statistics and contract match for one input column."""

    name: str
    null_frac: float
    distinct: int
    sample: list[str]
    matched_contract_column: str | None


@dataclass(frozen=True, slots=True)
class Profile:
    """Profile of an input frame against a contract.

    ``columns`` is keyed by input column name, in input order. ``missing`` lists unmatched
    contract columns (contract order); ``extra`` lists unmatched input columns (input order).
    """

    columns: dict[str, ColumnProfile]
    missing: list[str]
    extra: list[str]
    n_rows: int
    unit_issues: dict[str, str] = field(default_factory=dict)
    """Contract column -> why its values look like they are in the wrong unit (Ruling H2a)."""


def normalise_name(name: str) -> str:
    """``" O-Order Status "`` -> ``"o_order_status"``."""
    return _SEPARATORS.sub("_", name.strip()).lower()


def _candidates(name: str) -> list[str]:
    """Normalised spellings an input column may match, most specific first.

    The leaf of a flattened name is tried only for exactly one non-empty ``<container>.``
    prefix: that is the reader's own flattening of one nesting level, nothing deeper.
    """
    names = [normalise_name(name)]
    container, sep, leaf = name.partition(_NEST_SEP)
    if sep and container.strip() and leaf.strip() and _NEST_SEP not in leaf:
        names.append(normalise_name(leaf))
    return names


def match_columns(input_columns: list[str], contract: Contract) -> dict[str, str]:
    """Input column -> contract column, for every unambiguously matched input column."""
    declared = [c.name for c in contract.columns]
    exact = {name: name for name in input_columns if name in declared}
    taken = set(exact.values())
    by_target: dict[str, list[str]] = {}
    lookup = {normalise_name(c): c for c in declared}
    for name in input_columns:
        if name in exact:
            continue
        for cand in _candidates(name):
            target = lookup.get(cand)
            if target is not None:
                if target not in taken:
                    by_target.setdefault(target, []).append(name)
                break
    matched = exact | {names[0]: target for target, names in by_target.items() if len(names) == 1}
    return {name: matched[name] for name in input_columns if name in matched}


def _sample(series: pl.Series) -> list[str]:
    values = series.drop_nulls().unique(maintain_order=True).head(SAMPLE_SIZE)
    return [str(v) for v in values.to_list()]


def contract_sources(prof: Profile) -> dict[str, str]:
    """Contract column -> the input column the profile matched to it (unmatched ones absent)."""
    return {
        col.matched_contract_column: name
        for name, col in prof.columns.items()
        if col.matched_contract_column is not None
    }


def _number_texts(series: pl.Series) -> pl.Series:
    """Cells as text with grouping commas and whitespace removed; destroyed tokens -> null."""
    texts = series if series.dtype == pl.String else series.cast(pl.String, strict=False)
    destroyed = texts.str.strip_chars().is_in(pl.Series(sorted(SENTINEL_TOKENS)).implode())
    cleaned = texts.str.replace_all(_NUMBER_NOISE, "")
    return pl.select(pl.when(destroyed).then(None).otherwise(cleaned)).to_series()


def number_mask(series: pl.Series) -> pl.Series:
    """Per row: the cell reads as a plain number once grouping commas are removed (no nulls)."""
    return _number_texts(series).str.contains(_NUMBER).fill_null(False)


def number_values(series: pl.Series) -> pl.Series:
    """Per row: the cell's number as ``Float64`` where :func:`number_mask` holds, else null."""
    values = _number_texts(series).cast(pl.Float64, strict=False)
    return pl.select(pl.when(number_mask(series)).then(values)).to_series()


@dataclass(frozen=True, slots=True)
class Magnitude:
    """Size statistics of the numbers in a column (absolute values; see :func:`magnitude`)."""

    count: int
    whole_share: float
    median: float
    p5: float
    p95: float


def magnitude(series: pl.Series) -> Magnitude | None:
    """Statistics of the cells that read as plain numbers once grouping commas are removed.

    Each cell is taken as text (numeric dtypes are cast), whitespace and ``,`` are removed, and
    it counts when it is ``-?digits[.digits]`` and not a destroyed token (``"-1"``). A value is
    *whole* when it has no fractional part or only zeros after the point. ``None`` when no cell
    parses. Vectorised: O(rows), plus O(rows log rows) for the median and quantiles.
    """
    cleaned = _number_texts(series)
    numbers = cleaned.filter(number_mask(series))
    if numbers.len() == 0:
        return None
    values = numbers.cast(pl.Float64, strict=False).abs()
    whole = int(numbers.str.contains(_WHOLE).sum())
    return Magnitude(
        count=numbers.len(),
        whole_share=whole / numbers.len(),
        median=float(values.median()),  # type: ignore[arg-type]
        p5=float(values.quantile(0.05, "nearest")),  # type: ignore[arg-type]
        p95=float(values.quantile(0.95, "nearest")),  # type: ignore[arg-type]
    )


def unit_issue(series: pl.Series, spec: ColumnSpec) -> str | None:
    """Why ``series`` looks like whole paise for an INR decimal column, else ``None``.

    Fires when at least :data:`UNIT_MIN_VALUES` cells parse and at least
    :data:`UNIT_WHOLE_SHARE` of them are whole numbers (Ruling H2a). Genuine rupee amounts
    carry paise as decimals, so almost none of them are whole.
    """
    if spec.unit != UNIT_CHECKED or spec.dtype != "decimal":
        return None
    stats = magnitude(series)
    if stats is None or stats.count < UNIT_MIN_VALUES or stats.whole_share < UNIT_WHOLE_SHARE:
        return None
    share = int(stats.whole_share * 100)  # floored: never shows 98% for 97.9%
    return (
        f"values look like paise: {share}% whole numbers, median {stats.median:.0f}; "
        "expected INR with paise as decimals"
    )


def profile(df: pl.DataFrame, contract: Contract) -> Profile:
    """Profile ``df`` and match its columns to ``contract`` (see module docstring)."""
    matches = match_columns(list(df.columns), contract)
    n = df.height
    columns = {
        name: ColumnProfile(
            name=name,
            null_frac=(df[name].null_count() / n) if n else 0.0,
            distinct=df[name].drop_nulls().n_unique(),
            sample=_sample(df[name]),
            matched_contract_column=matches.get(name),
        )
        for name in df.columns
    }
    matched = set(matches.values())
    missing = [c.name for c in contract.columns if c.name not in matched]
    extra = [name for name in df.columns if name not in matches]
    units = {}
    for name, target in matches.items():
        if (detail := unit_issue(df[name], contract.column(target))) is not None:
            units[target] = detail
    ordered = {c.name: units[c.name] for c in contract.columns if c.name in units}
    return Profile(columns=columns, missing=missing, extra=extra, n_rows=n, unit_issues=ordered)
