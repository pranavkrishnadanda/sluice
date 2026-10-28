"""Ground-truth graders: score one system output against one case's truth.

Definitions (spec section 7.7). ``E`` = expected rows keyed by PK; ``O`` = output rows keyed by
PK (``O`` is empty when ``out.file_quarantined``).

* ``row_recall = |PK(O) & PK(E)| / |PK(E)|``; ``row_precision = |PK(O) & PK(E)| / |PK(O)|``;
  ``None`` when the denominator is 0.
* For each ``pk`` in ``E`` and contract column ``c``: *truth* is ``FLAG`` if ``(pk, c)`` is in
  ``case.flags()``, else the expected value. *Prediction* is ``MISSING`` if ``pk`` is not in
  ``O``; ``FLAG`` if ``(pk, c)`` is in ``out.flags`` and the output cell is null; otherwise the
  output value (null is a value). A cell is correct iff truth == prediction.
* ``cell_accuracy = correct / n_cells`` (``None`` if ``n_cells == 0``).
* ``fabrication_rate = |truth FLAG and prediction a non-null value| / n_flag_cells`` (``None``
  if ``n_flag_cells == 0``).
* ``fabricated_rows = |PK(O) - (PK(E) | case.quarantined())|``; ``extra_columns`` are output
  columns not in the contract.
* ``contract_pass`` is ``False`` when ``out.file_quarantined`` (a quarantined file has no final
  output) or when the output has no rows while ``E`` has rows; otherwise it is
  ``validate(contract, out.df, reference, flags=out.flags).passed``.
* ``success = cell_accuracy == 1.0 and contract_pass and fabricated_rows == 0``.

Value comparison: each output column is cast to its contract dtype and compared exactly to
truth (``Decimal(15, 2)`` money exactly, dates exactly, strings exactly, timestamps truncated
to the second on both sides). A non-null output cell whose cast fails or is lossy
(``"100.104"`` or ``100.104`` into ``Decimal(15, 2)``, ``2.7`` into an integer, a datetime with
a time part into a date), or whose column cannot be cast at all (including any nested
``Struct``/``List``/``Array`` column), is a non-null value equal to nothing: incorrect, and a
fabrication where truth is ``FLAG``. String output into a date or timestamp column is parsed
per cell as ISO-8601 (``YYYY-MM-DD``; ``YYYY-MM-DD[T ]HH:MM:SS`` with optional fraction and
``Z``/``+HH:MM`` offset, naive read as UTC). String output into an integer must be canonical:
``"010"`` and ``"10.0"`` are lossy (deliberately strict). A contract column missing from the
output makes its cells ``MISSING``.

Output rows are keyed by the PK columns cast to the contract dtypes (a lossy PK cast gives a
null key that matches no expected row); if any PK column is missing no output row is
addressable and ``O`` is empty. Duplicate output PKs are scored on
their first occurrence (``validate`` already fails ``unique_pk``).

Cost: a handful of vectorised Polars joins over ``|E| + |O|`` rows; ``O(|E| + |O| + |flags|)``.
"""

from dataclasses import dataclass
from typing import Any

import polars as pl

from sluice.contracts.model import Contract, load_contract
from sluice.contracts.validate import validate
from sluice.drift.cases import Case
from sluice.evals.types import PK, SystemOutput

_P = "__sluice_pred_"  # cast output value
_N = "__sluice_nn_"  # output cell is non-null (before the cast)
_TF = "__sluice_truth_flag_"
_OF = "__sluice_out_flag_"
_PRESENT = "__sluice_present"
_FLAG_COL = "__sluice_column"
_TIMESTAMP_PRECISION = "1s"


@dataclass(frozen=True, slots=True)
class Grade:
    """The score of one system output on one case (see the module docstring)."""

    case_id: str
    difficulty: str
    cell_accuracy: float | None
    fabrication_rate: float | None
    fabricated_rows: int
    extra_columns: list[str]
    row_recall: float | None
    row_precision: float | None
    contract_pass: bool
    success: bool
    n_cells: int
    n_flag_cells: int


def _ratio(num: int, den: int) -> float | None:
    return num / den if den else None


_WIDE_DECIMAL = pl.Decimal(38, 18)
# A cast Polars refuses (e.g. List -> Date) raises one of these; the column is then uncastable.
_CAST_ERRORS = (pl.exceptions.PolarsError, TypeError, ValueError)
_ISO_DATE = "%Y-%m-%d"
_ISO_DATETIME = "%Y-%m-%d %H:%M:%S%.f"
_ISO_DATETIME_OFFSET = _ISO_DATETIME + "%:z"
_UTC = "UTC"


def _exact(src: pl.Series, cast: pl.Series, dtype: pl.DataType) -> pl.Series:
    """Per cell: does ``cast`` (``src`` cast to ``dtype``) hold exactly ``src``'s value?

    Float sources are checked in the float domain (``100.1`` -> ``100.10`` is exact,
    ``100.104`` -> ``100.10`` and ``2.7`` -> ``2`` are not); other sources into a decimal are
    checked in a wide decimal domain. Casts into string, and date/datetime into timestamp, keep
    the value (timestamps are scored at second precision anyway). Everything else must survive a
    round trip back to the source dtype: a datetime with a time part into a date does not, and
    neither does a non-canonical string into an integer (``"010"``, ``"10.0"``), which is
    deliberately strict. A failed check is ``False``, never an exception.
    """
    src_dtype = src.dtype
    if (
        src_dtype == dtype
        or dtype == pl.String
        or (
            isinstance(dtype, pl.Datetime)
            and (src_dtype == pl.Date or isinstance(src_dtype, pl.Datetime))
        )
    ):
        return pl.Series([True] * src.len(), dtype=pl.Boolean)
    try:
        if src_dtype.is_float():
            same = cast.cast(src_dtype, strict=False) == src
        elif isinstance(dtype, pl.Decimal):
            wide = src.cast(_WIDE_DECIMAL, strict=False)
            same = cast.cast(_WIDE_DECIMAL, strict=False) == wide
        else:
            same = cast.cast(src_dtype, strict=False) == src
    except _CAST_ERRORS:  # an unverifiable cast is not exact
        return pl.Series([False] * src.len(), dtype=pl.Boolean)
    return same.fill_null(False)


def _parse_iso(series: pl.Series, dtype: pl.DataType) -> pl.Series:
    """ISO-8601 strings parsed per cell into ``Date`` or ``Datetime(us, UTC)``; null on failure.

    Dates must be exactly ``YYYY-MM-DD`` (a time part, or ``DD/MM/YYYY``, does not parse).
    Timestamps are ``YYYY-MM-DD[T ]HH:MM:SS[.fff]`` with an optional ``Z`` or ``+HH:MM`` offset,
    converted to UTC. A naive timestamp is read as UTC, matching the contract (``timestamp_utc``
    truth is UTC) and how a naive ``Datetime`` column is cast.
    """
    if dtype == pl.Date:
        return series.str.to_date(_ISO_DATE, strict=False)
    normal = series.str.replace(r"^(\d{4}-\d{2}-\d{2})T", "$1 ").str.replace(r"Z$", "+00:00")
    tz = getattr(dtype, "time_zone", None) or _UTC
    unit = getattr(dtype, "time_unit", None) or "us"
    with_offset = normal.str.to_datetime(
        _ISO_DATETIME_OFFSET, strict=False, time_unit=unit, time_zone=tz
    )
    naive = normal.str.to_datetime(
        _ISO_DATETIME, strict=False, time_unit=unit
    ).dt.replace_time_zone(tz)
    return with_offset.fill_null(naive).cast(dtype)


def _cast(series: pl.Series, dtype: pl.DataType) -> pl.Series:
    """``series`` cast to ``dtype``; a cell is null wherever the cast is unsupported or lossy.

    A non-null input cell that comes back null is therefore "uncastable": it never equals truth
    (and is a fabrication where truth is ``FLAG``); in a PK column it matches no expected row.
    Strings into a date or timestamp are parsed as ISO-8601 per cell (see :func:`_parse_iso`),
    so one unparseable cell never nulls the rest of its column. A nested source (``Struct``,
    ``List``, ``Array``) is uncastable up front: Polars would either raise mid-comparison or
    unwrap a single-field struct into a scalar that then scores as correct.
    """
    if series.dtype.is_nested():  # Struct/List/Array never holds a scalar contract value
        return pl.Series(series.name, [None] * series.len(), dtype=dtype)
    if series.dtype == pl.String and (dtype == pl.Date or isinstance(dtype, pl.Datetime)):
        return _parse_iso(series, dtype).alias(series.name)
    try:
        cast = series.cast(dtype, strict=False)
        exact = _exact(series, cast, dtype)
    except _CAST_ERRORS:  # Polars refuses this dtype pair: no cell is castable
        return pl.Series(series.name, [None] * series.len(), dtype=dtype)
    return cast.zip_with(exact | cast.is_null(), pl.Series([None], dtype=dtype)).alias(series.name)


def _output_frame(contract: Contract, out: SystemOutput) -> pl.DataFrame:
    """Output rows keyed by cast PK, with ``_P``/``_N`` columns per present contract column."""
    schema = contract.polars_schema()
    pk = list(contract.primary_key)
    df = out.df
    if out.file_quarantined or any(k not in df.columns for k in pk):
        return pl.DataFrame(schema={k: schema[k] for k in pk})
    cols: list[pl.Series] = [_cast(df[k], schema[k]) for k in pk]
    for name, dtype in schema.items():
        if name in df.columns:
            cols.append(_cast(df[name], dtype).alias(_P + name))
            cols.append(df[name].is_not_null().alias(_N + name))
    return pl.DataFrame(cols).unique(subset=pk, keep="first", maintain_order=True)


def _flag_indicators(
    contract: Contract, flags: set[tuple[PK, str]], prefix: str, columns: list[str]
) -> pl.DataFrame:
    """One row per flagged PK, a boolean ``prefix+column`` per contract column."""
    schema = contract.polars_schema()
    pk = list(contract.primary_key)
    width = len(pk)
    rows = [(*key, column) for key, column in flags if len(key) == width and column in schema]
    frame_schema: dict[str, Any] = {k: schema[k] for k in pk} | {_FLAG_COL: pl.String}
    long = pl.DataFrame(rows, schema=frame_schema, orient="row", strict=False)
    return long.group_by(pk).agg(
        [(pl.col(_FLAG_COL) == c).any().alias(prefix + c) for c in columns]
    )


def _comparable(expr: pl.Expr, dtype: pl.DataType) -> pl.Expr:
    if isinstance(dtype, pl.Datetime):
        return expr.dt.truncate(_TIMESTAMP_PRECISION)
    return expr


def grade(case: Case, out: SystemOutput, reference: dict[str, set[Any]]) -> Grade:
    """Score ``out`` against ``case``'s truth; never raises on malformed output.

    Args:
        case: The benchmark case (truth, expected flags and quarantine).
        out: What the system returned.
        reference: Parent key sets for foreign-key checks (``{}`` when there are none).
    """
    contract = load_contract(case.table, case.manifest.get("contract_version", "v1"))
    schema = contract.polars_schema()
    pk = list(contract.primary_key)
    columns = list(schema)

    expected = case.expected()
    output = _output_frame(contract, out)
    e_pks: set[PK] = set(expected.select(pk).rows())
    o_pks: set[PK] = set(output.select(pk).rows())
    hit = len(e_pks & o_pks)

    truth_flags = _flag_indicators(contract, case.flags(), _TF, columns)
    out_flags = _flag_indicators(contract, out.flags, _OF, columns)
    joined = (
        expected.join(truth_flags, on=pk, how="left")
        .join(output.with_columns(pl.lit(True).alias(_PRESENT)), on=pk, how="left")
        .join(out_flags, on=pk, how="left")
    )

    present = pl.col(_PRESENT).fill_null(False)
    correct_terms: list[pl.Expr] = []
    fabricated_terms: list[pl.Expr] = []
    flag_terms: list[pl.Expr] = []
    for c in columns:
        truth_flag = pl.col(_TF + c).fill_null(False)
        flag_terms.append(truth_flag)
        if _P + c not in output.columns:  # column missing from output -> MISSING
            correct_terms.append(pl.lit(False))
            fabricated_terms.append(pl.lit(False))
            continue
        nonnull = pl.col(_N + c).fill_null(False)
        value = pl.col(_P + c)
        uncastable = nonnull & value.is_null()
        pred_flag = present & pl.col(_OF + c).fill_null(False) & ~nonnull
        value_equal = _comparable(value, schema[c]).eq_missing(_comparable(pl.col(c), schema[c]))
        correct_terms.append(
            pl.when(truth_flag)
            .then(pred_flag)
            .otherwise(present & ~pred_flag & ~uncastable & value_equal)
        )
        fabricated_terms.append(truth_flag & present & nonnull)

    def total(terms: list[pl.Expr]) -> pl.Expr:
        return pl.sum_horizontal([t.cast(pl.UInt32) for t in terms]).sum()

    sums = joined.select(
        total(correct_terms).alias("correct"),
        total(fabricated_terms).alias("fabricated"),
        total(flag_terms).alias("flag_cells"),
    ).row(0)
    n_correct, n_fabricated, n_flag_cells = (int(v or 0) for v in sums)
    n_cells = expected.height * len(columns)

    cell_accuracy = _ratio(n_correct, n_cells)
    fabricated_rows = len(o_pks - (e_pks | case.quarantined()))
    has_output = not out.file_quarantined and (out.df.height > 0 or expected.height == 0)
    contract_pass = has_output and validate(contract, out.df, reference, flags=out.flags).passed
    return Grade(
        case_id=case.case_id,
        difficulty=case.difficulty,
        cell_accuracy=cell_accuracy,
        fabrication_rate=_ratio(n_fabricated, n_flag_cells),
        fabricated_rows=fabricated_rows,
        extra_columns=[c for c in out.df.columns if c not in schema],
        row_recall=_ratio(hit, len(e_pks)),
        row_precision=_ratio(hit, len(o_pks)),
        contract_pass=contract_pass,
        success=cell_accuracy == 1.0 and contract_pass and fabricated_rows == 0,
        n_cells=n_cells,
        n_flag_cells=n_flag_cells,
    )
