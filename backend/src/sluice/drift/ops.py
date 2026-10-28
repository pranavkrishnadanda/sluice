"""Seeded drift ops: break clean data the way real producers do, recording exact ground truth.

Every op has the signature ``op(state, rng, **params) -> None``, mutates ``state`` in place and
appends exactly one :class:`~sluice.drift.state.ManifestEntry`. All randomness comes from the
passed ``np.random.Generator``, so the same seed and op sequence give identical output.

Ops address columns by their *contract* name; the current name in ``state.df`` (after a
``rename`` or hinted ``unit_paise``) is looked up through ``state.current_name``. An op raises
:class:`DriftError` when its column is unknown, dropped, or no longer has a dtype it can act on
(e.g. ``unit_paise`` after ``indian_number_string``).

Row selection: ``k = round_half_up(frac * n)`` items are drawn without replacement. Ops whose
drift is part of the ground truth (``destroy``, ``fk_break``, ``unicode_text``) select *clean
primary keys* and apply the change to every ``df`` row carrying that key, so duplicate copies
never disagree with the truth. Presentation-only ops select ``df`` rows.

``rows_affected`` counts ``df`` rows whose cell (or row) the op changed; whole-column
conversions count non-null cells, except ``indian_number_string``, which counts only cells
whose text actually gains a group separator.

Complexity: every op is O(n) in the frame height (a few Python-level passes for the per-cell
string work); memory is O(n) for one rewritten column.
"""

import math
from collections.abc import Callable, Iterable
from decimal import Decimal

import numpy as np
import polars as pl

from sluice.contracts.model import ColumnSpec
from sluice.drift.state import DriftState, ManifestEntry, PkTuple

DESTROY_TOKENS: tuple[str, ...] = ("N/A", "", "-1", "###")
DATE_FORMATS: tuple[str, ...] = ("%d/%m/%Y", "%d %b %Y")
ISO_DATE = "%Y-%m-%d"
IST_ZONE = "Asia/Kolkata"
IST_FORMAT = "%Y-%m-%d %H:%M:%S%:z"
PAISE_PER_RUPEE = 100
MAX_PAD = 3
UNICODE_CHARS: tuple[str, ...] = ("é", "ñ", "ü")
FK_BREAK_RANGE = 1_000_000_000
WORDS: tuple[str, ...] = (
    "alpha",
    "amber",
    "basin",
    "cedar",
    "delta",
    "ember",
    "fjord",
    "grove",
    "harbor",
    "indigo",
    "juniper",
    "kestrel",
    "lagoon",
    "meadow",
    "nimbus",
    "orchid",
    "pebble",
    "quartz",
    "river",
    "saffron",
    "tundra",
    "umber",
    "valley",
    "willow",
)
MAX_WORDS = 3


class DriftError(ValueError):
    """An op cannot be applied to the current state (bad params, column or dtype)."""


# --- helpers ----------------------------------------------------------------------------------


def _spec(state: DriftState, column: str) -> ColumnSpec:
    try:
        return state.contract.column(column)
    except KeyError:
        raise DriftError(f"contract {state.contract.name!r} has no column {column!r}") from None


def _current(state: DriftState, column: str) -> str:
    _spec(state, column)
    if column not in state.current_name:
        raise DriftError(f"column {column!r} was dropped")
    return state.current_name[column]


def _require(state: DriftState, column: str, ok: Callable[[pl.DataType], bool], want: str) -> str:
    """Return ``column``'s current name, checking its current ``df`` dtype satisfies ``ok``."""
    name = _current(state, column)
    dtype = state.df.schema[name]
    if not ok(dtype):
        raise DriftError(f"column {column!r} has dtype {dtype}; expected {want}")
    return name


def _check_frac(frac: float) -> float:
    if not 0.0 <= frac <= 1.0:
        raise DriftError(f"frac must be in [0, 1], got {frac}")
    return float(frac)


def _count(frac: float, n: int) -> int:
    return min(n, math.floor(frac * n + 0.5))


def _pick(rng: np.random.Generator, n: int, k: int) -> list[int]:
    """``k`` distinct indices in ``range(n)``, ascending."""
    if k == 0:
        return []
    return sorted(int(i) for i in rng.choice(n, size=k, replace=False))


def _pick_rows(rng: np.random.Generator, candidates: list[int], frac: float) -> list[int]:
    return [candidates[i] for i in _pick(rng, len(candidates), _count(frac, len(candidates)))]


def _pick_pks(rng: np.random.Generator, candidates: list[PkTuple], frac: float) -> list[PkTuple]:
    return [candidates[i] for i in _pick(rng, len(candidates), _count(frac, len(candidates)))]


def _distinct_live_pks(state: DriftState) -> list[PkTuple]:
    """Distinct clean PKs of ``df`` rows not yet quarantined, in ``df`` row order.

    A row is live while its clean PK is still in ``expected`` (``fk_break`` is the only op that
    removes truth rows); ``state.quarantined`` holds drifted PKs and cannot be used here.
    """
    live = set(_expected_pks(state))
    return list(dict.fromkeys(pk for pk in state.row_pk if pk in live))


def _row_mask(state: DriftState, pks: Iterable[PkTuple]) -> pl.Series:
    chosen = set(pks)
    return pl.Series([pk in chosen for pk in state.row_pk], dtype=pl.Boolean)


def _expected_pks(state: DriftState) -> list[PkTuple]:
    return state.expected.select(state.contract.primary_key).rows()


def _null_expected(state: DriftState, column: str, pks: Iterable[PkTuple]) -> None:
    chosen = set(pks)
    mask = pl.Series([pk in chosen for pk in _expected_pks(state)], dtype=pl.Boolean)
    dtype = state.expected.schema[column]
    state.expected = state.expected.with_columns(
        pl.when(mask).then(pl.lit(None, dtype=dtype)).otherwise(pl.col(column)).alias(column)
    )


def _non_null_rows(state: DriftState, name: str) -> list[int]:
    return [i for i, v in enumerate(state.df[name].to_list()) if v is not None]


def _replace_column(state: DriftState, name: str, values: list[object], dtype: pl.DataType) -> None:
    state.df = state.df.with_columns(pl.Series(name, values, dtype=dtype))


def _record(
    state: DriftState, op: str, params: dict[str, object], columns: list[str], rows: int
) -> None:
    state.entries.append(ManifestEntry(op=op, params=params, columns=columns, rows_affected=rows))


def _is_ascii_letter(ch: str) -> bool:
    return ch.isascii() and ch.isalpha()


def _replace_nth_letter(text: str, nth: int, char: str) -> str:
    """Replace the ``nth`` (0-based) ASCII letter of ``text`` with ``char``."""
    seen = 0
    for i, ch in enumerate(text):
        if _is_ascii_letter(ch):
            if seen == nth:
                return text[:i] + char + text[i + 1 :]
            seen += 1
    raise DriftError(f"text has fewer than {nth + 1} ASCII letters: {text!r}")


def _plain(value: Decimal | int) -> str:
    """The ungrouped decimal rendering :func:`format_indian` starts from."""
    return format(value, "f") if isinstance(value, Decimal) else str(value)


def format_indian(value: Decimal | int) -> str:
    """Format a number with Indian digit grouping: ``Decimal("124500.50")`` -> ``"1,24,500.50"``.

    The last three integer digits form one group; groups of two follow leftwards. The fractional
    part is kept exactly as the Decimal carries it.
    """
    text = _plain(value)
    sign = "-" if text.startswith("-") else ""
    whole, dot, frac = text.lstrip("-").partition(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        pairs = []
        while head:
            pairs.insert(0, head[-2:])
            head = head[:-2]
        whole = ",".join([*pairs, tail])
    return f"{sign}{whole}{dot}{frac}"


# --- ops --------------------------------------------------------------------------------------


def rename(state: DriftState, rng: np.random.Generator, *, column: str, new_name: str) -> None:
    """Rename ``column`` (wherever it currently lives) to ``new_name``."""
    name = _current(state, column)
    if not new_name or new_name in state.df.columns:
        raise DriftError(f"cannot rename {column!r} to {new_name!r}: empty or exists")
    state.df = state.df.rename({name: new_name})
    state.current_name[column] = new_name
    _record(state, "rename", {"column": column, "new_name": new_name}, [column], state.df.height)


def indian_number_string(state: DriftState, rng: np.random.Generator, *, column: str) -> None:
    """Rewrite a numeric column as strings with Indian digit grouping.

    ``rows_affected`` counts only cells whose text differs from the plain decimal rendering
    (values of magnitude 1000 and up gain a comma; ``999.00`` is unchanged).
    """
    name = _require(
        state, column, lambda d: isinstance(d, pl.Decimal) or d.is_integer(), "decimal or integer"
    )
    raw = state.df[name].to_list()
    values = [None if v is None else format_indian(v) for v in raw]
    _replace_column(state, name, values, pl.String())
    rows = sum(v is not None and text != _plain(v) for v, text in zip(raw, values, strict=True))
    _record(state, "indian_number_string", {"column": column}, [column], rows)


def unit_paise(state: DriftState, rng: np.random.Generator, *, column: str, hinted: bool) -> None:
    """Multiply a rupee Decimal column by 100 into Int64 paise; if ``hinted``, add ``_paise``."""
    name = _require(
        state,
        column,
        lambda d: isinstance(d, pl.Decimal) and (d.scale or 0) <= 2,
        "decimal with scale <= 2",
    )
    if hinted and f"{name}_paise" in state.df.columns:
        raise DriftError(f"column {name}_paise already exists")
    state.df = state.df.with_columns(
        (pl.col(name) * PAISE_PER_RUPEE).cast(pl.Int64, strict=True).alias(name)
    )
    if hinted:
        state.df = state.df.rename({name: f"{name}_paise"})
        state.current_name[column] = f"{name}_paise"
    rows = state.df.height - state.df[state.current_name[column]].null_count()
    _record(state, "unit_paise", {"column": column, "hinted": bool(hinted)}, [column], rows)


def tz_ist(state: DriftState, rng: np.random.Generator, *, column: str) -> None:
    """Rewrite a UTC timestamp column as ``"YYYY-MM-DD HH:MM:SS+05:30"`` strings in IST.

    Raises :class:`DriftError` if any value has sub-second precision (the format would lose it).
    """
    name = _require(
        state,
        column,
        lambda d: isinstance(d, pl.Datetime) and d.time_zone is not None,
        "tz-aware datetime",
    )
    col = state.df[name]
    if (col.dt.microsecond().fill_null(0) != 0).any():
        raise DriftError(f"column {column!r} has sub-second values; tz_ist would lose them")
    state.df = state.df.with_columns(
        pl.col(name).dt.convert_time_zone(IST_ZONE).dt.strftime(IST_FORMAT).alias(name)
    )
    _record(state, "tz_ist", {"column": column}, [column], state.df.height - col.null_count())


def date_format_mix(
    state: DriftState,
    rng: np.random.Generator,
    *,
    column: str,
    frac: float,
    formats: list[str],
) -> None:
    """Render a Date column as strings: ``frac`` of rows in a random format from ``formats``,
    the rest ISO ``YYYY-MM-DD``."""
    frac = _check_frac(frac)
    formats = list(formats)
    if not formats or any(f not in DATE_FORMATS for f in formats):
        raise DriftError(f"formats must be a non-empty subset of {DATE_FORMATS}, got {formats}")
    name = _require(state, column, lambda d: d == pl.Date, "date")
    rows = _pick_rows(rng, list(range(state.df.height)), frac)
    choice = np.full(state.df.height, -1, dtype=np.int64)
    choice[rows] = rng.integers(len(formats), size=len(rows))
    which = pl.Series(choice)
    expr = pl.col(name).dt.strftime(ISO_DATE)
    for i, fmt in enumerate(formats):
        expr = pl.when(which == i).then(pl.col(name).dt.strftime(fmt)).otherwise(expr)
    nulls = state.df[name].null_count()
    state.df = state.df.with_columns(expr.alias(name))
    params = {"column": column, "frac": frac, "formats": formats}
    _record(state, "date_format_mix", params, [column], state.df.height - nulls)


def whitespace(state: DriftState, rng: np.random.Generator, *, column: str, frac: float) -> None:
    """Pad ``frac`` of non-null string cells with 1-3 spaces on each side."""
    frac = _check_frac(frac)
    name = _require(state, column, lambda d: d == pl.String, "string")
    rows = _pick_rows(rng, _non_null_rows(state, name), frac)
    pads = rng.integers(1, MAX_PAD + 1, size=(len(rows), 2))
    values = state.df[name].to_list()
    for row, (left, right) in zip(rows, pads.tolist(), strict=True):
        values[row] = " " * left + values[row] + " " * right
    _replace_column(state, name, values, pl.String())
    _record(state, "whitespace", {"column": column, "frac": frac}, [column], len(rows))


def case_noise(state: DriftState, rng: np.random.Generator, *, column: str, frac: float) -> None:
    """Lower-case ``frac`` of non-null cells of an enum column."""
    frac = _check_frac(frac)
    if _spec(state, column).enum is None:
        raise DriftError(f"case_noise needs an enum column; {column!r} has no enum")
    name = _require(state, column, lambda d: d == pl.String, "string")
    rows = _pick_rows(rng, _non_null_rows(state, name), frac)
    values = state.df[name].to_list()
    for row in rows:
        values[row] = values[row].lower()
    _replace_column(state, name, values, pl.String())
    _record(state, "case_noise", {"column": column, "frac": frac}, [column], len(rows))


def destroy(
    state: DriftState, rng: np.random.Generator, *, column: str, frac: float, token: str
) -> None:
    """Replace the cells of ``frac`` of clean PKs with ``token``; flag them, null them in truth.

    ``"-1"`` on an integer column stays an integer sentinel; otherwise the column becomes String.
    Primary-key columns cannot be destroyed (the truth must stay keyed).
    """
    frac = _check_frac(frac)
    if token not in DESTROY_TOKENS:
        raise DriftError(f"token must be one of {DESTROY_TOKENS}, got {token!r}")
    if column in state.contract.primary_key:
        raise DriftError(f"cannot destroy primary key column {column!r}")
    name = _current(state, column)
    candidates = [pk for pk in _distinct_live_pks(state) if (pk, column) not in state.flags]
    chosen = _pick_pks(rng, candidates, frac)
    mask = _row_mask(state, chosen)
    if token == "-1" and state.df.schema[name].is_integer():
        replacement = pl.lit(-1, dtype=state.df.schema[name])
        current = pl.col(name)
    else:
        replacement = pl.lit(token, dtype=pl.String)
        current = pl.col(name).cast(pl.String)
    state.df = state.df.with_columns(pl.when(mask).then(replacement).otherwise(current).alias(name))
    state.flags.update((pk, column) for pk in chosen)
    _null_expected(state, column, chosen)
    params = {"column": column, "frac": frac, "token": token}
    _record(state, "destroy", params, [column], int(mask.sum()))


def duplicate_rows(state: DriftState, rng: np.random.Generator, *, frac: float) -> None:
    """Append exact copies of ``frac`` of the current ``df`` rows."""
    frac = _check_frac(frac)
    rows = _pick_rows(rng, list(range(state.df.height)), frac)
    if rows:
        state.df = pl.concat([state.df, state.df[rows]])
        state.row_pk.extend(state.row_pk[i] for i in rows)
    _record(state, "duplicate_rows", {"frac": frac}, [], len(rows))


def fk_break(state: DriftState, rng: np.random.Generator, *, column: str, frac: float) -> None:
    """Set the FK of ``frac`` of clean PKs to distinct negative ints; quarantine those rows.

    ``state.quarantined`` records each broken row's *drifted* PK (the clean PK with the new FK
    value substituted when the FK is part of the PK, as in lineitem): the broken key is all a
    system can see. Every ``df`` copy of a broken row carries the same drifted PK. Quarantined
    rows leave ``expected`` and any flags they carried are dropped.
    """
    frac = _check_frac(frac)
    if column not in state.contract.foreign_keys:
        raise DriftError(f"{column!r} is not a foreign key of {state.contract.name!r}")
    name = _require(state, column, lambda d: d.is_integer(), "integer")
    chosen = _pick_pks(rng, _distinct_live_pks(state), frac)
    broken = (-(rng.choice(FK_BREAK_RANGE, size=len(chosen), replace=False) + 1)).tolist()
    new_fk = dict(zip(chosen, broken, strict=True))
    values = state.df[name].to_list()
    rows = 0
    for i, pk in enumerate(state.row_pk):
        if pk in new_fk:
            values[i] = new_fk[pk]
            rows += 1
    _replace_column(state, name, values, state.df.schema[name])
    pk_cols = state.contract.primary_key
    state.quarantined.update(
        tuple(new_fk[pk] if col == column else v for col, v in zip(pk_cols, pk, strict=True))
        for pk in chosen
    )
    keep = pl.Series([pk not in new_fk for pk in _expected_pks(state)], dtype=pl.Boolean)
    state.expected = state.expected.filter(keep)
    state.flags = {(pk, col) for pk, col in state.flags if pk not in new_fk}
    _record(state, "fk_break", {"column": column, "frac": frac}, [column], rows)


def drop_column(state: DriftState, rng: np.random.Generator, *, column: str) -> None:
    """Drop ``column`` from ``df``; flag every expected cell of it and null it in truth."""
    if column in state.contract.primary_key:
        raise DriftError(f"cannot drop primary key column {column!r}")
    name = _current(state, column)
    state.df = state.df.drop(name)
    del state.current_name[column]
    pks = _expected_pks(state)
    state.flags.update((pk, column) for pk in pks)
    _null_expected(state, column, pks)
    _record(state, "drop_column", {"column": column}, [column], state.df.height)


def add_column(state: DriftState, rng: np.random.Generator, *, name: str) -> None:
    """Add an uncontracted String column ``name`` of 1-3 random lower-case words per cell."""
    if not name or name in state.df.columns:
        raise DriftError(f"cannot add column {name!r}: empty or exists")
    n = state.df.height
    counts = rng.integers(1, MAX_WORDS + 1, size=n).tolist()
    picks = rng.integers(len(WORDS), size=sum(counts)).tolist()
    values, at = [], 0
    for c in counts:
        values.append(" ".join(WORDS[j] for j in picks[at : at + c]))
        at += c
    state.df = state.df.with_columns(pl.Series(name, values, dtype=pl.String))
    _record(state, "add_column", {"name": name}, [name], n)


def unicode_text(state: DriftState, rng: np.random.Generator, *, column: str, frac: float) -> None:
    """Replace one ASCII letter with é/ñ/ü in ``frac`` of eligible cells, in ``df`` *and* truth.

    This is content, not drift: ``expected`` changes identically. The same letter (by ordinal
    among the cell's ASCII letters) is replaced in every ``df`` copy, so padding or case noise
    applied before or after does not misalign it. Eligible: non-null truth cells with an ASCII
    letter that are not flagged.
    """
    frac = _check_frac(frac)
    if _spec(state, column).dtype != "string":
        raise DriftError(f"unicode_text needs a string column; {column!r} is not")
    name = _require(state, column, lambda d: d == pl.String, "string")
    truth = state.expected[column].to_list()
    exp_pks = _expected_pks(state)
    candidates = [
        pk
        for pk, v in zip(exp_pks, truth, strict=True)
        if v is not None and (pk, column) not in state.flags and any(map(_is_ascii_letter, v))
    ]
    chosen = _pick_pks(rng, candidates, frac)
    truth_by_pk = dict(zip(exp_pks, truth, strict=True))
    edits: dict[PkTuple, tuple[int, str]] = {}
    for pk in chosen:
        letters = sum(map(_is_ascii_letter, truth_by_pk[pk]))
        edits[pk] = (int(rng.integers(letters)), UNICODE_CHARS[int(rng.integers(3))])
    state.expected = state.expected.with_columns(
        pl.Series(
            column,
            [
                _replace_nth_letter(v, *edits[pk]) if pk in edits else v
                for pk, v in zip(exp_pks, truth, strict=True)
            ],
            dtype=pl.String,
        )
    )
    values = state.df[name].to_list()
    rows = 0
    for i, pk in enumerate(state.row_pk):
        if pk in edits:
            values[i] = _replace_nth_letter(values[i] or "", *edits[pk])
            rows += 1
    _replace_column(state, name, values, pl.String())
    _record(state, "unicode_text", {"column": column, "frac": frac}, [column], rows)


OPS: dict[str, Callable[..., None]] = {
    op.__name__: op
    for op in (
        rename,
        indian_number_string,
        unit_paise,
        tz_ist,
        date_format_mix,
        whitespace,
        case_noise,
        destroy,
        duplicate_rows,
        fk_break,
        drop_column,
        add_column,
        unicode_text,
    )
}
