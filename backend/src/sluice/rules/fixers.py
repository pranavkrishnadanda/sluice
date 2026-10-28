"""Deterministic rule repairs: the no-LLM baseline. Rules never fabricate a value.

Rules, in this order:

1. Matched input columns are renamed to their contract names (see :mod:`sluice.rules.profile`).
2. Unmatched (extra) input columns are dropped.
3. Whitespace is stripped from cells of enum columns and of typed (non-string) columns.
   Free-text string columns (no enum) are left verbatim: their whitespace can be real data
   (TPC-H comments carry leading/trailing spaces) and stripping it would alter true values.
4. Enum cells are upper-cased when that, and only that, makes them valid.
5. Sentinel tokens (``"N/A"``, ``""``, ``"-1"``, ``"###"``, compared after stripping) in
   non-nullable columns become null and are flagged.
6. Dates parse from ISO ``YYYY-MM-DD``, and from ``DD/MM/YYYY`` only for a day-first contract
   locale (``en_IN``). Timestamps parse from ISO 8601 *with* an explicit UTC offset (``Z``,
   ``+05:30``, ...) and are converted to UTC; a naive timestamp's zone is never guessed.
7. Lossless casts: integers from plain digit strings (range-checked); decimals from plain
   ``123.45`` strings with at most two fractional digits. A money column (one with a ``unit``)
   also requires the fractional part, because a bare integer there may be in paise. Indian digit
   grouping (``1,24,500.50``) and paise are never converted.
8. Any cell that still fails to parse, or violates its contract enum/min/max, becomes null and is
   flagged. A null cell in a non-nullable column is flagged too.
9. Missing contract columns are added as null and flagged for every row.
10. Exact duplicate rows are dropped, then rows with an already-seen primary key (first kept).
11. Rows whose foreign key is missing from the reference key set are moved to ``quarantined``.

Rows whose primary key cannot be parsed cannot be keyed, flagged or deduplicated and are dropped
(and reported in ``actions``). A frame missing a primary-key column raises
:class:`UnkeyableInputError`.

Complexity: O(rows x columns) - one Python-level pass per cell for parsing (no regex
backtracking: every pattern is linear), plus Polars hash-based dedupe and FK anti-join.
Memory: O(rows x columns) for one rewritten copy of the frame.
"""

import re
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import polars as pl

from sluice.contracts.model import MONEY_PRECISION, MONEY_SCALE, ColumnSpec, Contract
from sluice.evals.types import PK
from sluice.rules.profile import Profile

SENTINEL_TOKENS: frozenset[str] = frozenset({"N/A", "", "-1", "###"})
DAY_FIRST_LOCALES: frozenset[str] = frozenset({"en_IN"})
INT_BOUNDS: dict[str, tuple[int, int]] = {
    "int32": (-(2**31), 2**31 - 1),
    "int64": (-(2**63), 2**63 - 1),
}
MAX_MONEY_INT_DIGITS = MONEY_PRECISION - MONEY_SCALE
_MONEY_QUANTUM = Decimal(1).scaleb(-MONEY_SCALE)

_INT_RE = re.compile(r"-?\d+", re.ASCII)
_DECIMAL_RE = re.compile(rf"-?\d+(\.\d{{1,{MONEY_SCALE}}})?", re.ASCII)
_MONEY_RE = re.compile(rf"-?\d+\.\d{{1,{MONEY_SCALE}}}", re.ASCII)
_ISO_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}", re.ASCII)
_DMY_RE = re.compile(r"\d{1,2}/\d{1,2}/\d{4}", re.ASCII)
_TS_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(\.\d{1,6})?(Z|[+-]\d{2}:\d{2})", re.ASCII
)
_DMY_FORMAT = "%d/%m/%Y"
_FLAG_PREFIX = "__flag__"
_ROW_INDEX = "__row__"
_FLAG_COUNT = "__n_flags__"
_RAW_ROW = "__raw_row__"

# Why a cell became null + flagged.
SENTINEL = "sentinel"
UNPARSEABLE = "unparseable"
OUT_OF_DOMAIN = "out_of_domain"
NULL_INPUT = "null_input"


class UnkeyableInputError(ValueError):
    """The input lacks a primary-key column, so no row can be keyed, flagged or deduplicated."""


@dataclass(frozen=True, slots=True)
class RuleResult:
    """Output of :func:`apply_rules`.

    Attributes:
        df: Rows in the exact contract schema.
        flags: ``(pk, column)`` cells set null because they could not be recovered.
        quarantined: PKs of rows removed for foreign-key misses.
        actions: Human-readable log of what each rule did, in rule order.
        dropped_unkeyed_rows: Rows dropped because their primary key could not be parsed.
    """

    df: pl.DataFrame
    flags: set[tuple[PK, str]]
    quarantined: set[PK]
    actions: list[str]
    dropped_unkeyed_rows: int = 0


# --- per-cell parsing -------------------------------------------------------------------------


class _Invalid(Exception):
    """A cell cannot be converted losslessly."""


def _parse_int(text: str, spec: ColumnSpec) -> int:
    if not _INT_RE.fullmatch(text):
        raise _Invalid
    value = int(text)
    lo, hi = INT_BOUNDS[spec.dtype]
    if not lo <= value <= hi:
        raise _Invalid
    return value


def _parse_decimal(text: str, spec: ColumnSpec) -> Decimal:
    pattern = _MONEY_RE if spec.unit is not None else _DECIMAL_RE
    if not pattern.fullmatch(text) or len(text.lstrip("-").split(".")[0]) > MAX_MONEY_INT_DIGITS:
        raise _Invalid
    return Decimal(text).quantize(_MONEY_QUANTUM)


def _date_parser(locale: str) -> Callable[[str, ColumnSpec], date]:
    day_first = locale in DAY_FIRST_LOCALES

    def parse(text: str, spec: ColumnSpec) -> date:
        try:
            if _ISO_DATE_RE.fullmatch(text):
                return date.fromisoformat(text)
            if day_first and _DMY_RE.fullmatch(text):
                return datetime.strptime(text, _DMY_FORMAT).date()
        except ValueError:
            pass
        raise _Invalid

    return parse


def _parse_timestamp(text: str, spec: ColumnSpec) -> datetime:
    if not _TS_RE.fullmatch(text):
        raise _Invalid
    try:
        return datetime.fromisoformat(text).astimezone(UTC)
    except ValueError:
        raise _Invalid from None


def _parse_string(text: str, spec: ColumnSpec) -> str:
    return text


def _parser(spec: ColumnSpec, locale: str) -> Callable[[str, ColumnSpec], Any]:
    match spec.dtype:
        case "int64" | "int32":
            return _parse_int
        case "decimal":
            return _parse_decimal
        case "date":
            return _date_parser(locale)
        case "timestamp_utc":
            return _parse_timestamp
        case _:
            return _parse_string


def _in_domain(value: Any, spec: ColumnSpec) -> bool:
    if spec.enum is not None and value not in spec.enum:
        return False
    convert = Decimal if spec.dtype == "decimal" else float
    if spec.min is not None and value < convert(str(spec.min)):
        return False
    return spec.max is None or value <= convert(str(spec.max))


# --- per-column repair ------------------------------------------------------------------------


def _as_text(series: pl.Series) -> list[str | None] | None:
    """Cells rendered as the strings a text file would carry; ``None`` for unsupported dtypes.

    Integers, decimals (fixed-point), dates and datetimes (ISO 8601, offset only when the value
    is zone-aware) are rendered exactly, so a typed Parquet column takes the same path as text.
    """
    dtype = series.dtype
    if dtype == pl.String:
        return series.to_list()
    if dtype.is_integer():
        return [None if v is None else str(v) for v in series.to_list()]
    if isinstance(dtype, pl.Decimal):
        return [None if v is None else format(v, "f") for v in series.to_list()]
    if dtype == pl.Date or isinstance(dtype, pl.Datetime):
        return [None if v is None else v.isoformat() for v in series.to_list()]
    return None


@dataclass(slots=True)
class _Column:
    values: list[Any]
    flagged: list[bool]
    counts: Counter[str]


def _repair_column(spec: ColumnSpec, series: pl.Series, locale: str) -> _Column:
    """Apply rules 3-8 to one matched column."""
    texts = _as_text(series)
    counts: Counter[str] = Counter()
    if texts is None:  # e.g. float or nested types: no lossless rule applies
        counts["unsupported_dtype"] = series.len() - series.null_count()
        counts[NULL_INPUT] = series.null_count()
        return _Column([None] * series.len(), [True] * series.len(), counts)
    parse = _parser(spec, locale)
    free_text = spec.dtype == "string" and spec.enum is None
    values: list[Any] = []
    flagged: list[bool] = []
    for raw in texts:
        value, why = _repair_cell(raw, spec, parse, free_text, counts)
        values.append(value)
        flagged.append(why is not None)
        if why is not None:
            counts[why] += 1
    return _Column(values, flagged, counts)


def _repair_cell(
    raw: str | None,
    spec: ColumnSpec,
    parse: Callable[[str, ColumnSpec], Any],
    free_text: bool,
    counts: Counter[str],
) -> tuple[Any, str | None]:
    """``(value, reason_flagged_or_None)`` for one cell."""
    if raw is None:
        return None, (None if spec.nullable else NULL_INPUT)
    text = raw.strip()
    if not spec.nullable and text in SENTINEL_TOKENS:
        return None, SENTINEL
    if free_text:
        text = raw
    elif text != raw:
        counts["stripped"] += 1
    if spec.enum is not None and text not in spec.enum and text.upper() in spec.enum:
        text = text.upper()
        counts["upper_cased"] += 1
    try:
        value = parse(text, spec)
    except _Invalid:
        return None, UNPARSEABLE
    if not _in_domain(value, spec):
        return None, OUT_OF_DOMAIN
    return value, None


def _column_actions(name: str, counts: Counter[str]) -> list[str]:
    labels = {
        "stripped": "cell(s) whitespace-stripped",
        "upper_cased": "enum cell(s) upper-cased",
        SENTINEL: "sentinel token(s) -> null + flag",
        NULL_INPUT: "null cell(s) in a non-nullable column flagged",
        UNPARSEABLE: "unparseable cell(s) -> null + flag",
        OUT_OF_DOMAIN: "cell(s) outside the contract enum/range -> null + flag",
        "unsupported_dtype": "cell(s) of an unsupported dtype -> null + flag",
    }
    return [f"{name}: {counts[key]} {label}" for key, label in labels.items() if counts[key]]


# --- frame-level rules ------------------------------------------------------------------------


def _flag_col(name: str) -> str:
    return f"{_FLAG_PREFIX}{name}"


def _select_matched(df: pl.DataFrame, prof: Profile, actions: list[str]) -> pl.DataFrame:
    """Rules 1-2: rename matched columns to contract names, drop the rest."""
    exprs = []
    for name, col in prof.columns.items():
        target = col.matched_contract_column
        if target is None:
            continue
        if target != name:
            actions.append(f"renamed {name!r} -> {target!r}")
        exprs.append(pl.col(name).alias(target))
    actions += [f"dropped extra column {name!r}" for name in prof.extra]
    return df.select(exprs)


def _repair_frame(df: pl.DataFrame, contract: Contract, actions: list[str]) -> pl.DataFrame:
    """Rules 3-9: typed contract columns plus one boolean flag column per contract column."""
    schema = contract.polars_schema()
    data: list[pl.Series] = []
    flags: list[pl.Series] = []
    for spec in contract.columns:
        if spec.name not in df.columns:
            data.append(pl.Series(spec.name, [None] * df.height, dtype=schema[spec.name]))
            flags.append(pl.Series(_flag_col(spec.name), [True] * df.height, dtype=pl.Boolean))
            actions.append(f"added missing column {spec.name!r} as null; flagged every row")
            continue
        col = _repair_column(spec, df[spec.name], contract.locale)
        data.append(pl.Series(spec.name, col.values, dtype=schema[spec.name]))
        flags.append(pl.Series(_flag_col(spec.name), col.flagged, dtype=pl.Boolean))
        actions += _column_actions(spec.name, col.counts)
    return pl.DataFrame([*data, *flags])


def _drop_unkeyed(
    frame: pl.DataFrame, contract: Contract, actions: list[str]
) -> tuple[pl.DataFrame, int]:
    keyed = pl.all_horizontal(pl.col(k).is_not_null() for k in contract.primary_key)
    out = frame.filter(keyed)
    if dropped := frame.height - out.height:
        actions.append(f"dropped {dropped} row(s) whose primary key could not be parsed")
    return out, dropped


def _dedupe(
    frame: pl.DataFrame, contract: Contract, actions: list[str], carry: tuple[str, ...] = ()
) -> pl.DataFrame:
    """Rule 10: exact duplicates (data and flags) first, then one row per PK, preferring the
    row that carries the most flags. ``carry`` columns ride along but never count toward
    "exact duplicate" (used by :func:`kept_raw_rows` to carry the raw row index)."""
    subset = [c for c in frame.columns if c not in carry] if carry else None
    exact = frame.unique(subset=subset, keep="first", maintain_order=True)  # data and flags
    if n := frame.height - exact.height:
        actions.append(f"dropped {n} exact duplicate row(s)")
    # Among rows sharing a PK keep the one with the most flags (earliest on ties), so a
    # null + flagged cell is never replaced by a look-alike null that carries no flag. The
    # sort is stable and the original row order is restored afterwards: deterministic.
    flag_cols = [_flag_col(c.name) for c in contract.columns]
    by_pk = (
        exact.with_row_index(_ROW_INDEX)
        .with_columns(pl.sum_horizontal(flag_cols).alias(_FLAG_COUNT))
        .sort([_FLAG_COUNT, _ROW_INDEX], descending=[True, False])
        .unique(subset=list(contract.primary_key), keep="first", maintain_order=True)
        .sort(_ROW_INDEX)
        .drop(_ROW_INDEX, _FLAG_COUNT)
    )
    if n := exact.height - by_pk.height:
        actions.append(f"dropped {n} row(s) repeating an earlier primary key")
    return by_pk


def _quarantine_fk(
    frame: pl.DataFrame,
    contract: Contract,
    reference: dict[str, set[Any]],
    actions: list[str],
) -> tuple[pl.DataFrame, set[PK]]:
    """Rule 11: rows whose FK is not in its reference key set leave the frame."""
    quarantined: set[PK] = set()
    pk = list(contract.primary_key)
    for column, (ref_table, ref_col) in contract.foreign_keys.items():
        ref_key = f"{ref_table}.{ref_col}"
        if ref_key not in reference:
            continue
        allowed = pl.Series(list(reference[ref_key]), dtype=frame.schema[column], strict=False)
        miss = pl.col(column).is_not_null() & ~pl.col(column).is_in(allowed.implode())
        bad = frame.filter(miss).select(pk).rows()
        if bad:
            quarantined.update(bad)
            frame = frame.filter(~miss)
            actions.append(f"quarantined {len(bad)} row(s): {column} not in {ref_key}")
    return frame, quarantined


def _collect_flags(frame: pl.DataFrame, contract: Contract) -> set[tuple[PK, str]]:
    pk = list(contract.primary_key)
    return {
        (key, spec.name)
        for spec in contract.columns
        for key in frame.filter(pl.col(_flag_col(spec.name))).select(pk).rows()
    }


def cell_texts(series: pl.Series) -> list[str | None] | None:
    """Raw cells as the text the rules parse (see :func:`_as_text`); ``None`` if unsupported."""
    return _as_text(series)


def kept_raw_rows(df: pl.DataFrame, contract: Contract, prof: Profile) -> dict[PK, int]:
    """Clean primary key -> index of the raw ``df`` row :func:`apply_rules` keeps for it.

    Runs the same rules 1-10 (matching, cell parsing, unkeyed-row drop, dedupe) with the raw
    row index carried through, so the choice among duplicate keys is exactly the rules' own.
    Rows dropped as unkeyed or as duplicates are absent. FK quarantine (rule 11) is not applied:
    quarantined rows carry no flags.

    Complexity: O(rows x columns), the cost of one rules pass.

    Raises:
        UnkeyableInputError: A primary-key column has no matching input column.
    """
    _require_keys(contract, prof)
    frame = _repair_frame(_select_matched(df, prof, []), contract, [])
    frame = frame.with_row_index(_RAW_ROW)
    frame, _ = _drop_unkeyed(frame, contract, [])
    frame = _dedupe(frame, contract, [], carry=(_RAW_ROW,))
    return {
        tuple(row[:-1]): row[-1] for row in frame.select([*contract.primary_key, _RAW_ROW]).rows()
    }


def flagged_raw_rows(df: pl.DataFrame, contract: Contract, prof: Profile) -> dict[str, set[int]]:
    """Matched contract column -> indices of the raw ``df`` rows whose own cell rules 3-9 flag.

    Per raw row, before the unkeyed-row drop and dedupe (rules 9-10): every copy of a duplicate
    row is listed when its cell is, so a rewrite applied to these rows keeps duplicates
    identical. Uses the same per-cell repair as :func:`apply_rules`. Contract columns with no
    matching input column are absent.

    Complexity: O(rows x columns), one cell-repair pass.
    """
    frame = _repair_frame(_select_matched(df, prof, []), contract, [])
    out: dict[str, set[int]] = {}
    for spec in contract.columns:
        if spec.name in prof.missing:
            continue
        rows = frame[_flag_col(spec.name)].arg_true().to_list()
        if rows:
            out[spec.name] = set(rows)
    return out


def _require_keys(contract: Contract, prof: Profile) -> None:
    if missing_pk := [k for k in contract.primary_key if k in prof.missing]:
        raise UnkeyableInputError(f"primary-key column(s) not found in input: {missing_pk}")


def apply_rules(
    df: pl.DataFrame, contract: Contract, prof: Profile, reference: dict[str, set[Any]]
) -> RuleResult:
    """Repair ``df`` toward ``contract`` with deterministic, lossless rules only.

    Args:
        df: The input as read by :func:`sluice.io.read_input`. Not modified.
        contract: Target contract.
        prof: ``profile(df, contract)``; supplies the column matching.
        reference: Parent key sets keyed ``"<table>.<column>"`` for foreign-key checks.

    Returns:
        A :class:`RuleResult` whose ``df`` has the exact contract schema; every flagged cell
        is null in it, and no flagged or quarantined key is outside the input.

    Raises:
        UnkeyableInputError: A primary-key column has no matching input column.
    """
    _require_keys(contract, prof)
    actions: list[str] = []
    selected = _select_matched(df, prof, actions)
    frame = _repair_frame(selected, contract, actions)
    frame, unkeyed = _drop_unkeyed(frame, contract, actions)
    frame = _dedupe(frame, contract, actions)
    frame, quarantined = _quarantine_fk(frame, contract, reference, actions)
    flags = _collect_flags(frame, contract)
    out = frame.select([c.name for c in contract.columns])
    return RuleResult(out, flags, quarantined, actions, dropped_unkeyed_rows=unkeyed)
