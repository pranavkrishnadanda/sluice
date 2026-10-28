"""Validate a Polars DataFrame against a :class:`~sluice.contracts.model.Contract`.

Per-column checks (``dtype``, ``not_null``, ``enum``, ``min``, ``max``) and ``unique_pk`` run
through a ``pandera.polars.DataFrameSchema`` built from the contract. The column-set checks
(``missing_column``, ``extra_column``) and ``foreign_key`` are done here directly.

Every violation carries the primary-key tuples of the offending rows as Python-native values,
so callers can join violations back to rows or to repair flags.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import pandera.polars as pa
import polars as pl
from pandera.errors import SchemaErrors

from sluice.contracts.model import ColumnSpec, Contract

MAX_PKS_IN_MESSAGE = 5

PkTuple = tuple[Any, ...]
Flags = set[tuple[PkTuple, str]]

# Checks whose result is meaningless when the column has the wrong dtype.
_VALUE_CHECKS = frozenset({"enum", "min", "max"})
_UNIQUE_CHECKS = ("multiple_fields_uniqueness", "field_uniqueness")


@dataclass(frozen=True, slots=True)
class Violation:
    """One failed check. ``column`` is ``None`` for table-level checks (``unique_pk``)."""

    column: str | None
    check: str
    pk_values: list[PkTuple]
    message: str


@dataclass(frozen=True, slots=True)
class ValidationReport:
    """Outcome of :func:`validate`; ``passed`` is true iff there are no violations."""

    passed: bool
    violations: list[Violation]


def _bound(spec: ColumnSpec, value: float) -> int | float | Decimal:
    """Contract bound in the column's own numeric domain (exact for money)."""
    if spec.dtype == "decimal":
        return Decimal(str(value))
    return value


def _column_checks(spec: ColumnSpec) -> tuple[list[pa.Check], list[str]]:
    """Pandera checks for ``spec`` and, position for position, our check names."""
    checks: list[pa.Check] = []
    names: list[str] = []
    if spec.enum is not None:
        checks.append(pa.Check.isin(spec.enum))
        names.append("enum")
    if spec.min is not None:
        checks.append(pa.Check.ge(_bound(spec, spec.min)))
        names.append("min")
    if spec.max is not None:
        checks.append(pa.Check.le(_bound(spec, spec.max)))
        names.append("max")
    return checks, names


def _message(
    column: str | None,
    check: str,
    pks: list[PkTuple],
    detail: str = "",
    n_rows: int | None = None,
) -> str:
    """Human-readable violation text.

    ``n_rows`` gives the offending-row count when PKs are unavailable (PK column missing).
    """
    where = f"column {column!r}" if column is not None else "table"
    text = f"{where}: check {check!r} failed"
    if detail:
        text += f" ({detail})"
    if pks:
        shown = ", ".join(str(pk) for pk in pks[:MAX_PKS_IN_MESSAGE])
        more = f" (+{len(pks) - MAX_PKS_IN_MESSAGE} more)" if len(pks) > MAX_PKS_IN_MESSAGE else ""
        text += f" for {len(pks)} row(s); PKs: {shown}{more}"
    elif n_rows:
        text += f" for {n_rows} row(s); PKs unavailable (primary key column missing)"
    return text


def _dedupe(pks: Iterable[PkTuple]) -> list[PkTuple]:
    return list(dict.fromkeys(pks))


class _PkLookup:
    """Row index -> primary-key tuple of Python-native values."""

    def __init__(self, df: pl.DataFrame, pk: tuple[str, ...]) -> None:
        self._frame = df.select(pk) if all(c in df.columns for c in pk) else None

    @property
    def available(self) -> bool:
        return self._frame is not None

    def rows(self, indices: list[int]) -> list[PkTuple]:
        if self._frame is None or not indices:
            return []
        return self._frame[indices].rows()

    def mask(self, mask: pl.Expr, df: pl.DataFrame) -> list[PkTuple]:
        if self._frame is None:
            return []
        return self._frame.filter(df.select(mask).to_series()).rows()


def _column_set_violations(contract: Contract, df: pl.DataFrame) -> list[Violation]:
    declared = {c.name for c in contract.columns}
    out = [
        Violation(c.name, "missing_column", [], _message(c.name, "missing_column", []))
        for c in contract.columns
        if c.name not in df.columns
    ]
    out += [
        Violation(name, "extra_column", [], _message(name, "extra_column", []))
        for name in df.columns
        if name not in declared
    ]
    return out


def _pandera_violations(
    contract: Contract, df: pl.DataFrame, pks: _PkLookup, flags: Flags
) -> list[Violation]:
    present = [c for c in contract.columns if c.name in df.columns]
    expected_dtypes = contract.polars_schema()
    check_names: dict[str, list[str]] = {}
    columns: dict[str, pa.Column] = {}
    for spec in present:
        checks, names = _column_checks(spec)
        check_names[spec.name] = names
        columns[spec.name] = pa.Column(
            expected_dtypes[spec.name], checks=checks, nullable=spec.nullable
        )
    schema = pa.DataFrameSchema(
        columns,
        unique=list(contract.primary_key) if pks.available else None,
        strict=False,
        coerce=False,
    )
    try:
        schema.validate(df.select(list(columns)), lazy=True)
    except SchemaErrors as exc:
        cases = exc.failure_cases
    else:
        return []

    # (column, check) -> offending row indices, in first-seen order.
    hits: dict[tuple[str | None, str], list[int]] = {}
    for row in cases.iter_rows(named=True):
        raw_check: str = row["check"]
        column: str | None = row["column"]
        if raw_check.startswith(_UNIQUE_CHECKS):
            key = (None, "unique_pk")
        elif raw_check.startswith("dtype("):
            key = (column, "dtype")
        elif raw_check == "not_nullable":
            key = (column, "not_null")
        elif row["check_number"] is not None and column in check_names:
            key = (column, check_names[column][row["check_number"]])
        else:
            raise RuntimeError(f"unmapped pandera failure: column={column!r} check={raw_check!r}")
        indices = hits.setdefault(key, [])
        if row["index"] is not None:
            indices.append(row["index"])

    bad_dtype = {col for col, check in hits if check == "dtype"}
    order = {c.name: i for i, c in enumerate(contract.columns)}
    check_order = ["dtype", "not_null", "enum", "min", "max", "unique_pk"]

    def sort_key(key: tuple[str | None, str]) -> tuple[int, int]:
        col, check = key
        return (order.get(col, len(order)) if col else len(order), check_order.index(check))

    out: list[Violation] = []
    for column, check in sorted(hits, key=sort_key):
        if column in bad_dtype and check in _VALUE_CHECKS:
            continue  # value checks on a mistyped column are noise; dtype already reported
        indices = hits[(column, check)]
        pk_values = _dedupe(pks.rows(indices))
        n_rows = len(set(indices))
        detail = ""
        # Flags are keyed by PK: without a PK lookup no cell can match a flag, so a not_null
        # violation is then reported as-is (pk_values=[], row count in the message).
        if check == "not_null" and pks.available:
            pk_values = [pk for pk in pk_values if (pk, column) not in flags]
            if not pk_values:
                continue
        elif check == "dtype":
            detail = f"expected {expected_dtypes[column]}, got {df.schema[column]}"
        msg = _message(column, check, pk_values, detail, n_rows)
        out.append(Violation(column, check, pk_values, msg))
    return out


def _fk_violations(
    contract: Contract,
    df: pl.DataFrame,
    pks: _PkLookup,
    reference: dict[str, set[Any]],
) -> list[Violation]:
    out: list[Violation] = []
    for column, (ref_table, ref_col) in contract.foreign_keys.items():
        ref_key = f"{ref_table}.{ref_col}"
        if column not in df.columns or ref_key not in reference:
            continue
        allowed = pl.Series(list(reference[ref_key]), dtype=df.schema[column], strict=False)
        mask = pl.col(column).is_not_null() & ~pl.col(column).is_in(allowed.implode())
        n_rows = int(df.select(mask.sum()).item())
        if n_rows:
            pk_values = pks.mask(mask, df)
            msg = _message(column, "foreign_key", pk_values, f"not in {ref_key}", n_rows)
            out.append(Violation(column, "foreign_key", pk_values, msg))
    return out


def validate(
    contract: Contract,
    df: pl.DataFrame,
    reference: dict[str, set[Any]] | None = None,
    flags: Flags | None = None,
) -> ValidationReport:
    """Check ``df`` against ``contract``.

    Args:
        contract: The table contract.
        df: Data to check. Not modified.
        reference: Key sets of referenced tables, keyed ``"<table>.<column>"``. A foreign key
            is checked only when its referenced key set is supplied.
        flags: ``(pk_tuple, column)`` cells flagged as unrecoverable; such cells are exempt
            from ``not_null`` (a flagged cell is legitimately null).

    Returns:
        A report with one :class:`Violation` per failed ``(column, check)``. Order is
        deterministic: column-set checks, then per-column checks in contract order, then
        ``unique_pk``, then ``foreign_key``.
    """
    pks = _PkLookup(df, contract.primary_key)
    violations = _column_set_violations(contract, df)
    violations += _pandera_violations(contract, df, pks, flags or set())
    if reference:
        violations += _fk_violations(contract, df, pks, reference)
    return ValidationReport(passed=not violations, violations=violations)
