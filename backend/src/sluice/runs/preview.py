"""The run's before/after preview (``docs/api/contract.md``, "Preview"): build it, page it.

:func:`build_preview` turns the uploaded file and the system output into one frame, one row per
raw row in file order, stored as Parquet by the API worker; :func:`page_preview` serves a page of
it as a :class:`~sluice.api.schemas.DataPreview`. Both are pure (no I/O).

Frame layout: ``pk`` (String), ``quarantined`` (Boolean), ``reason`` (String, null unless
quarantined) and ``cells``, a struct with one field per contract column (contract order), each a
struct ``{before: String, after: String, flagged: Boolean}``.

* ``pk``: the row's clean primary key (parsed by the rules exactly as the load did), parts
  joined with ``|`` in key order (``"1|3"``). A row whose key cannot be parsed shows its raw
  key text instead and is held back (the rules drop it).
* ``before``: the raw text of the source column mapped to that contract column
  (``meta["mapping"]``, e.g. ``amt`` under ``o_totalprice``), ``null`` for a null cell or an
  unmapped column. Without ``meta["mapping"]`` (the rules-only system) the rules' own column
  match is used.
* ``after``: the loaded value rendered canonically: decimals in fixed point (``"124500.50"``),
  ISO dates, ISO-8601 UTC timestamps ending ``Z``; ``null`` when loaded as null or not loaded.
* ``flagged``: the cell is in ``out.flags`` (loaded as null for review).
* ``quarantined`` / ``reason``: the row was held back: its key is in ``out.quarantined``
  (``meta["quarantine_reasons"]``), the whole file was quarantined, or its key was unparseable.

Complexity: O(rows x columns) time and memory (one cell-repair pass for the keys, then one pass
over the rows); a page is O(limit x columns).
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, Final

import polars as pl

from sluice.api.schemas import DataPreview, PreviewCell, PreviewRow
from sluice.contracts.model import Contract
from sluice.evals.types import PK, SystemOutput, pk_text
from sluice.rules.fixers import UnkeyableInputError, cell_texts, raw_row_keys
from sluice.rules.profile import contract_sources, profile

__all__ = ["PREVIEW_MAX_LIMIT", "build_preview", "page_preview"]

PREVIEW_MAX_LIMIT: Final = 1_000
"""Server maximum (and default) for ``?limit=`` (``PREVIEW_MAX_LIMIT`` in ``types.ts``)."""

KEY_SEP: Final = "|"
DEFAULT_REASON: Final = "rejected"
FILE_QUARANTINED: Final = "file quarantined"
UNKEYED_REASON: Final = "primary key could not be parsed"
_CELL_FIELDS: Final = ("before", "after", "flagged")


def render(value: Any) -> str | None:
    """A loaded value as canonical text (decimal fixed point, ISO date, UTC timestamp + ``Z``)."""
    if value is None:
        return None
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, datetime):  # before ``date``: a datetime is a date
        moment = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
        return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def _sources(raw: pl.DataFrame, out: SystemOutput, contract: Contract) -> dict[str, str | None]:
    """Contract column -> raw column (``meta["mapping"]``, else the rules' own match)."""
    mapping = out.meta.get("mapping")
    if not isinstance(mapping, Mapping):
        matched = contract_sources(profile(raw, contract))
        mapping = {c.name: matched.get(c.name) for c in contract.columns}
    sources: dict[str, str | None] = {}
    for spec in contract.columns:
        source = mapping.get(spec.name)
        sources[spec.name] = source if isinstance(source, str) and source in raw.columns else None
    return sources


def _raw_texts(raw: pl.DataFrame, source: str | None) -> list[str | None]:
    if source is None:
        return [None] * raw.height
    series = raw[source]
    texts = cell_texts(series)
    if texts is None:  # a dtype the rules do not read (e.g. a float): Polars' own text
        texts = series.cast(pl.String, strict=False).to_list()
    return texts


def _keys(raw: pl.DataFrame, contract: Contract, sources: dict[str, str | None]) -> list[PK | None]:
    """Each raw row's clean key; all ``None`` when a key column is unmapped."""
    if any(sources[k] is None for k in contract.primary_key):
        return [None] * raw.height
    mapped = pl.DataFrame([raw[src].alias(col) for col, src in sources.items() if src is not None])
    try:
        return raw_row_keys(mapped, contract, profile(mapped, contract))
    except UnkeyableInputError:  # defensive: every key column is mapped above
        return [None] * raw.height


def build_preview(raw: pl.DataFrame, out: SystemOutput, contract: Contract) -> pl.DataFrame:
    """The before/after preview of one run (layout in the module docstring).

    Args:
        raw: The uploaded file as :func:`sluice.io.read_input` reads it.
        out: The system's output for that file.
        contract: The target contract.
    """
    names = [c.name for c in contract.columns]
    sources = _sources(raw, out, contract)
    before = {name: _raw_texts(raw, sources[name]) for name in names}
    keys = _keys(raw, contract, sources)
    raw_keys = [_raw_texts(raw, sources[k]) for k in contract.primary_key]

    key_index = [names.index(k) for k in contract.primary_key]
    loaded: dict[PK, list[str | None]] = {}
    if not out.file_quarantined:
        for row in out.df.select(names).rows():
            loaded[tuple(row[i] for i in key_index)] = [render(v) for v in row]
    flagged: dict[str, set[PK]] = {name: set() for name in names}
    for pk, column in out.flags:
        flagged.setdefault(column, set()).add(tuple(pk))
    reasons = out.meta.get("quarantine_reasons") or {}
    held_back = {tuple(pk) for pk in out.quarantined}
    file_reason = str(out.meta.get("file_quarantine_reason") or FILE_QUARANTINED)

    pks: list[str] = []
    quarantined: list[bool] = []
    why: list[str | None] = []
    after: dict[str, list[str | None]] = {name: [] for name in names}
    flags: dict[str, list[bool]] = {name: [] for name in names}
    for i, key in enumerate(keys):
        if key is None:
            pks.append(KEY_SEP.join(part[i] or "" for part in raw_keys))
        else:
            pks.append(KEY_SEP.join(str(part) for part in key))
        values = loaded.get(key) if key is not None else None
        if out.file_quarantined:
            reason: str | None = file_reason
        elif key is None:
            reason = UNKEYED_REASON
        elif key in held_back:
            reason = str(reasons.get(pk_text(key), DEFAULT_REASON))
        else:
            reason = None
        quarantined.append(reason is not None)
        why.append(reason)
        for j, name in enumerate(names):
            after[name].append(None if values is None else values[j])
            flags[name].append(key is not None and values is not None and key in flagged[name])

    columns: dict[str, pl.Series] = {
        "pk": pl.Series(pks, dtype=pl.String),
        "quarantined": pl.Series(quarantined, dtype=pl.Boolean),
        "reason": pl.Series(why, dtype=pl.String),
    }
    for j, name in enumerate(names):
        columns[f"{j}.before"] = pl.Series(before[name], dtype=pl.String)
        columns[f"{j}.after"] = pl.Series(after[name], dtype=pl.String)
        columns[f"{j}.flagged"] = pl.Series(flags[name], dtype=pl.Boolean)
    cells = pl.struct(
        [
            pl.struct([pl.col(f"{j}.{f}").alias(f) for f in _CELL_FIELDS]).alias(name)
            for j, name in enumerate(names)
        ]
    ).alias("cells")
    return pl.DataFrame(columns, height=raw.height).select("pk", "quarantined", "reason", cells)


def page_preview(
    preview: pl.DataFrame, contract: Contract, *, offset: int, limit: int
) -> DataPreview:
    """One page of a stored preview: at most :data:`PREVIEW_MAX_LIMIT` rows from ``offset``.

    ``nextOffset`` is ``None`` after the last page (or past the end); ``totalRows`` is every
    row of the uploaded file.

    Raises:
        ValueError: ``offset < 0`` or ``limit < 1``.
    """
    if offset < 0:
        raise ValueError("offset must be >= 0")
    if limit < 1:
        raise ValueError("limit must be >= 1")
    limit = min(limit, PREVIEW_MAX_LIMIT)
    names = [c.name for c in contract.columns]
    rows = [
        PreviewRow(
            pk=row["pk"],
            quarantined=row["quarantined"],
            reason=row["reason"],
            cells={name: PreviewCell(**row["cells"][name]) for name in names},
        )
        for row in preview.slice(offset, limit).to_dicts()
    ]
    end = offset + limit
    return DataPreview(
        columns=names,
        rows=rows,
        total_rows=preview.height,
        next_offset=end if end < preview.height else None,
    )
