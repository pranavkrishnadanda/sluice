"""Format- and encoding-tolerant input reader.

CSV and JSON are read with every column as ``pl.String``; typing is the pipeline's job.
Parquet is passed through with its stored schema, after :func:`check_decoded_size` has checked
its footer: a few KB of Parquet can declare billions of RLE/dictionary-encoded rows, so the size
on disk says nothing about the memory a full read takes (CSV and JSON are roughly linear in it).
"""

import io
import json
from pathlib import Path
from typing import Any, Final

import polars as pl
import pyarrow.parquet as pq

MAX_PARQUET_ROWS: Final = 5_000_000
"""Most rows a Parquet input may declare (Ruling F3p4)."""
MAX_PARQUET_DECODED_BYTES: Final = 1024**3
"""Largest estimated decoded size of a Parquet input: 1 GiB (Ruling F3p4)."""
MIN_CELL_BYTES: Final = 8
"""Lower bound on the in-memory cost of one cell, used in the decoded-size estimate."""


class DecodedTooLarge(ValueError):
    """A Parquet input declares more rows or decoded bytes than the caps allow."""


_CSV_ENCODINGS: tuple[str, ...] = ("utf-8-sig", "latin-1")
_KEY_SEP = "."


def read_input(path: Path) -> pl.DataFrame:
    """Read a CSV, JSON or Parquet file into a DataFrame, chosen by file extension.

    Args:
        path: File to read. Supported extensions: ``.csv``, ``.json``, ``.parquet``.

    Returns:
        The data. CSV/JSON columns are all ``pl.String``; Parquet keeps its schema.

    Raises:
        DecodedTooLarge: If a Parquet file's footer declares too much data (nothing is read).
        ValueError: If the extension is unsupported or a JSON file is not an array of objects.
    """
    match path.suffix.lower():
        case ".csv":
            return _read_csv(path)
        case ".json":
            return _read_json(path)
        case ".parquet":
            check_decoded_size(path)
            return pl.read_parquet(path)
        case other:
            raise ValueError(f"Unsupported input format {other!r} for {path}")


def check_decoded_size(path: Path) -> None:
    """Refuse a Parquet file whose footer declares too much data, reading the footer only.

    The estimate is the larger of the footer's uncompressed column bytes (summed over row
    groups) and ``rows x columns x MIN_CELL_BYTES``: encoded page sizes under-report repeated
    values, which decode to one value per row. O(row groups) time, O(footer) memory. Other
    formats pass unchecked; an unreadable footer raises pyarrow's error (``unparseable``).

    Raises:
        DecodedTooLarge: ``num_rows > MAX_PARQUET_ROWS`` or the estimate is over
            ``MAX_PARQUET_DECODED_BYTES``.
    """
    if path.suffix.lower() != ".parquet":
        return
    meta = pq.read_metadata(path)
    if meta.num_rows > MAX_PARQUET_ROWS:
        raise DecodedTooLarge(f"{meta.num_rows} rows declared; the limit is {MAX_PARQUET_ROWS}")
    stored = sum(meta.row_group(i).total_byte_size for i in range(meta.num_row_groups))
    estimate = max(stored, meta.num_rows * meta.num_columns * MIN_CELL_BYTES)
    if estimate > MAX_PARQUET_DECODED_BYTES:
        limit = MAX_PARQUET_DECODED_BYTES
        raise DecodedTooLarge(f"about {estimate} bytes decoded; the limit is {limit}")


def _decode(raw: bytes) -> str:
    """Decode bytes with the first encoding that succeeds (latin-1 never fails)."""
    for encoding in _CSV_ENCODINGS[:-1]:
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode(_CSV_ENCODINGS[-1])


def _read_csv(path: Path) -> pl.DataFrame:
    text = _decode(path.read_bytes())
    return pl.read_csv(io.StringIO(text), infer_schema=False)


def _flatten(obj: dict[str, Any], prefix: str = "") -> dict[str, str | None]:
    """Flatten nested objects into ``.``-joined keys, stringifying leaf values."""
    flat: dict[str, str | None] = {}
    for key, value in obj.items():
        name = f"{prefix}{_KEY_SEP}{key}" if prefix else str(key)
        if isinstance(value, dict):
            flat.update(_flatten(value, name))
        elif value is None:
            flat[name] = None
        elif isinstance(value, str):
            flat[name] = value
        elif isinstance(value, bool | list):
            flat[name] = json.dumps(value)  # JSON spelling: true/false, [..]
        else:
            flat[name] = str(value)
    return flat


def _read_json(path: Path) -> pl.DataFrame:
    with path.open("rb") as fh:
        data = json.load(fh)
    if not isinstance(data, list) or not all(isinstance(row, dict) for row in data):
        raise ValueError(f"JSON input must be an array of objects: {path}")
    rows = [_flatten(row) for row in data]
    columns = list(dict.fromkeys(key for row in rows for key in row))
    schema = dict.fromkeys(columns, pl.String)
    return pl.DataFrame(rows, schema=schema)
