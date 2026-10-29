"""The uploaded file's raw rows, as text, one page at a time (``GET /runs/{id}/input``).

Available from the moment the run is created, so a run that never loads (held back, failed or
still queued) can still show the user their file. Columns are the file's own (after the reader's
JSON flattening), not the contract's; every cell is text or ``None``.

Reading: CSV and JSON are read in full with :func:`sluice.io.read_input` (every column is a
String there; the 100 MB upload limit bounds the frame), then sliced. Parquet first passes
:func:`sluice.io.check_decoded_size` (the decompression-bomb guard), then only the page's rows
are decoded (``scan_parquet(...).slice``) and its row count comes from the footer.

Cost: O(file) per page for CSV/JSON (memory: the decoded file), O(page) for Parquet; rendering
is O(limit x columns).
"""

from __future__ import annotations

from pathlib import Path

import polars as pl
import pyarrow.parquet as pq

from sluice.api.schemas import RawInput
from sluice.io import check_decoded_size, read_input

__all__ = ["read_raw_page", "texts"]


def texts(series: pl.Series) -> list[str | None]:
    """``series`` as text: Strings as they are, other scalars cast (``true``, ``3.25``), and
    anything that cannot be cast (nested, binary) through ``str``. Nulls stay ``None``."""
    if series.dtype == pl.String:
        return series.to_list()
    try:
        return series.cast(pl.String).to_list()
    except pl.exceptions.PolarsError:
        return [None if value is None else str(value) for value in series.to_list()]


def read_raw_page(path: Path, offset: int, limit: int) -> RawInput:
    """Rows ``[offset, offset + limit)`` of the file at ``path`` (any accepted format).

    Raises:
        DecodedTooLarge: A Parquet footer declares too much data (nothing is decoded).
    """
    if path.suffix.lower() == ".parquet":
        check_decoded_size(path)
        total = pq.read_metadata(path).num_rows
        page = pl.scan_parquet(path).slice(offset, limit).collect()
    else:
        frame = read_input(path)
        total = frame.height
        page = frame.slice(offset, limit)
    columns = [texts(page[name]) for name in page.columns]
    rows = [list(row) for row in zip(*columns, strict=True)] if columns else []
    end = offset + limit
    return RawInput(
        columns=list(page.columns),
        rows=rows,
        total_rows=total,
        next_offset=end if end < total else None,
    )
