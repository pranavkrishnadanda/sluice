"""Column profiling and deterministic input-to-contract column matching.

Matching is deliberately literal: an input column matches a contract column when its name is
identical, or identical after normalisation (strip, lower-case, ``-``/space -> ``_``). A name the
reader produced by flattening nested JSON (``details.o_clerk``) is also tried by its leaf key
(``o_clerk``): that undoes the reader's own flattening and recovers the producer's key verbatim.
There is no fuzzy or semantic matching (``orderdate`` never matches ``o_orderdate``); that is
the agent's job.

Ties: an exact name match always wins. Otherwise, if two or more input columns normalise to the
same contract column, none of them is matched (ambiguity is never resolved by guessing).

Complexity: O(rows x columns) for the null/distinct statistics, O(columns) for matching.
"""

import re
from dataclasses import dataclass

import polars as pl

from sluice.contracts.model import Contract

SAMPLE_SIZE = 5
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


def normalise_name(name: str) -> str:
    """``" O-Order Status "`` -> ``"o_order_status"``."""
    return _SEPARATORS.sub("_", name.strip()).lower()


def _candidates(name: str) -> list[str]:
    """Normalised spellings an input column may match, most specific first."""
    names = [normalise_name(name)]
    if _NEST_SEP in name:
        names.append(normalise_name(name.rsplit(_NEST_SEP, 1)[1]))
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
    return Profile(columns=columns, missing=missing, extra=extra, n_rows=n)
