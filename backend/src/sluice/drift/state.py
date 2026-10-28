"""Drift state: the drifted frame plus the exact ground truth it must be scored against.

A :class:`DriftState` starts from a clean, contract-conforming frame. Drift ops (see
:mod:`sluice.drift.ops`) mutate ``df`` and, where the drift destroys or removes information,
record that in ``expected`` / ``flags`` / ``quarantined``. Every op appends one
:class:`ManifestEntry` so a run can be replayed and audited.

Primary-key tuples hold Python-native ``int``/``str`` values, so they survive a JSON round-trip.
``flags`` and ``row_pk`` use the *clean* PK values (stable across renames and rewrites);
``quarantined`` uses the *drifted* PK, because a quarantined row's key was rewritten by
``fk_break`` and the drifted key is all a system can report.
"""

from dataclasses import dataclass, field
from typing import Any

import polars as pl

from sluice.contracts.model import Contract

PkTuple = tuple[Any, ...]


@dataclass(frozen=True, slots=True)
class ManifestEntry:
    """One applied drift op.

    ``params`` holds the op's keyword arguments (JSON-native values only); ``columns`` names the
    contract columns touched (for ``add_column``, the new column's name).
    """

    op: str
    params: dict[str, Any]
    columns: list[str]
    rows_affected: int


@dataclass(slots=True)
class DriftState:
    """Mutable drift bookkeeping for one table.

    Attributes:
        contract: The table's contract.
        df: The drifted frame; evolves with every op.
        expected: Clean truth in the exact contract schema. Destroyed or dropped cells are null
            (even in non-nullable columns); quarantined rows are removed.
        flags: ``(clean_pk, contract_column)`` cells a healer is expected to flag.
        quarantined: PKs of rows expected to land in quarantine, as they appear in the
            *drifted* input (after ``fk_break`` rewrote a PK-member foreign key).
        current_name: Contract column -> its current name in ``df`` (absent once dropped).
        entries: The manifest, one entry per applied op.
        row_pk: Clean PK of each ``df`` row, aligned with ``df`` row order. Lets ops map drifted
            rows (including duplicates) back to ground truth.
    """

    contract: Contract
    df: pl.DataFrame
    expected: pl.DataFrame
    flags: set[tuple[PkTuple, str]] = field(default_factory=set)
    quarantined: set[PkTuple] = field(default_factory=set)
    current_name: dict[str, str] = field(default_factory=dict)
    entries: list[ManifestEntry] = field(default_factory=list)
    row_pk: list[PkTuple] = field(default_factory=list)

    @classmethod
    def start(cls, contract: Contract, clean_df: pl.DataFrame) -> DriftState:
        """Begin drifting ``clean_df``, which must match the contract schema exactly.

        Raises:
            ValueError: If the schema (names, order, dtypes) differs from the contract, or the
                primary key is not unique.
        """
        schema = contract.polars_schema()
        if dict(clean_df.schema) != schema:
            raise ValueError(
                f"clean frame schema {dict(clean_df.schema)} != contract schema {schema}"
            )
        pks = clean_df.select(contract.primary_key).rows()
        if len(set(pks)) != len(pks):
            raise ValueError(f"contract {contract.name!r}: primary key is not unique")
        return cls(
            contract=contract,
            df=clean_df.clone(),
            expected=clean_df.clone(),
            current_name={c: c for c in schema},
            row_pk=pks,
        )
