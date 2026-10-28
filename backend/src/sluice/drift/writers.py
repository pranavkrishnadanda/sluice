"""Deterministic writers for drifted case inputs.

CSV and JSON carry no types, so every typed cell is rendered explicitly and the same way on every
run: ``Decimal`` as a plain fixed-point string (``"1245.00"``), dates as ISO ``YYYY-MM-DD``,
datetimes as ISO 8601 with their UTC offset (``"1996-01-02T05:20:00+00:00"``). Columns an op has
already turned into strings are written verbatim. JSON keeps integers as numbers. Parquet keeps
the frame's dtypes as they are.

Output depends only on the frame (column order, values) and the arguments, never on time or dict
iteration order, so the same seed gives byte-identical files.

Complexity: O(rows x columns) Python-level cell conversions; memory O(file size).
"""

import csv
import io
import json
from collections.abc import Sequence
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

import polars as pl

Format = Literal["csv", "json", "json_nested", "parquet"]
Encoding = Literal["utf-8", "utf-8-sig", "latin-1"]

FORMATS: tuple[str, ...] = ("csv", "json", "json_nested", "parquet")
ENCODINGS: tuple[str, ...] = ("utf-8", "utf-8-sig", "latin-1")
NESTED_KEY = "details"
_EXTENSIONS: dict[str, str] = {
    "csv": "csv",
    "json": "json",
    "json_nested": "json",
    "parquet": "parquet",
}


def extension(fmt: str) -> str:
    """File extension (no dot) used for ``fmt``; raises ``ValueError`` on an unknown format."""
    try:
        return _EXTENSIONS[fmt]
    except KeyError:
        raise ValueError(f"unknown format {fmt!r}; expected one of {FORMATS}") from None


def write_input(
    df: pl.DataFrame,
    path_stem: Path,
    fmt: Format,
    encoding: Encoding,
    *,
    pk_columns: Sequence[str] = (),
) -> Path:
    """Write ``df`` to ``<path_stem>.<ext>`` and return the path.

    Args:
        df: Frame to write.
        path_stem: Target path without extension; its parent must exist.
        fmt: ``csv``, ``json`` (array of flat objects), ``json_nested`` (``pk_columns`` at top
            level, every other column under ``"details"``) or ``parquet``.
        encoding: Text encoding for CSV/JSON; ignored for parquet.
        pk_columns: Current names of the primary-key columns; required for ``json_nested``.

    Raises:
        ValueError: Unknown format/encoding, or ``json_nested`` without valid ``pk_columns``.
        UnicodeEncodeError: A cell cannot be represented in ``encoding``.
        TypeError: A column dtype has no defined text rendering.
    """
    path = path_stem.with_name(f"{path_stem.name}.{extension(fmt)}")
    if encoding not in ENCODINGS:
        raise ValueError(f"unknown encoding {encoding!r}; expected one of {ENCODINGS}")
    if fmt == "parquet":
        df.write_parquet(path)
        return path
    if fmt == "csv":
        text = _csv_text(df)
    else:
        rows = _json_rows(df)
        if fmt == "json_nested":
            rows = _nest(rows, list(df.columns), pk_columns)
        text = _json_text(rows)
    path.write_bytes(text.encode(encoding))
    return path


def _render(value: Any) -> Any:
    """JSON-native rendering of one cell (strings for Decimal/date/datetime)."""
    match value:
        case None | str() | bool() | int():
            return value
        case Decimal():
            return format(value, "f")
        case datetime() | date():  # datetime first: it is a subclass of date
            return value.isoformat()
        case float():
            return value
        case _:
            raise TypeError(f"cannot render cell of type {type(value).__name__}")


def _column_values(series: pl.Series) -> list[Any]:
    return [_render(v) for v in series.to_list()]


def _json_rows(df: pl.DataFrame) -> list[dict[str, Any]]:
    columns = {name: _column_values(df[name]) for name in df.columns}
    return [{name: columns[name][i] for name in df.columns} for i in range(df.height)]


def _nest(
    rows: list[dict[str, Any]], columns: list[str], pk_columns: Sequence[str]
) -> list[dict[str, Any]]:
    pk = list(pk_columns)
    if not pk or any(c not in columns for c in pk):
        raise ValueError(f"json_nested needs pk_columns present in the frame, got {pk}")
    if NESTED_KEY in pk:
        raise ValueError(f"a primary-key column may not be named {NESTED_KEY!r}")
    rest = [c for c in columns if c not in pk]
    return [{**{k: row[k] for k in pk}, NESTED_KEY: {c: row[c] for c in rest}} for row in rows]


def _json_text(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "[]\n"
    body = ",\n".join(json.dumps(row, ensure_ascii=False) for row in rows)
    return f"[\n{body}\n]\n"


def _text_cell(value: Any) -> str:
    rendered = _render(value)
    if rendered is None:
        return ""
    if isinstance(rendered, bool):
        return "true" if rendered else "false"
    return str(rendered)


def _csv_text(df: pl.DataFrame) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(df.columns)
    columns = [[_text_cell(v) for v in df[name].to_list()] for name in df.columns]
    writer.writerows(zip(*columns, strict=True))
    return buf.getvalue()
