"""Format- and encoding-tolerant input reader.

CSV and JSON are read with every column as ``pl.String``; typing is the pipeline's job.
Parquet is passed through with its stored schema.
"""

import io
import json
from pathlib import Path
from typing import Any

import polars as pl

_CSV_ENCODINGS: tuple[str, ...] = ("utf-8-sig", "latin-1")
_KEY_SEP = "."


def read_input(path: Path) -> pl.DataFrame:
    """Read a CSV, JSON or Parquet file into a DataFrame, chosen by file extension.

    Args:
        path: File to read. Supported extensions: ``.csv``, ``.json``, ``.parquet``.

    Returns:
        The data. CSV/JSON columns are all ``pl.String``; Parquet keeps its schema.

    Raises:
        ValueError: If the extension is unsupported or a JSON file is not an array of objects.
    """
    match path.suffix.lower():
        case ".csv":
            return _read_csv(path)
        case ".json":
            return _read_json(path)
        case ".parquet":
            return pl.read_parquet(path)
        case other:
            raise ValueError(f"Unsupported input format {other!r} for {path}")


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
