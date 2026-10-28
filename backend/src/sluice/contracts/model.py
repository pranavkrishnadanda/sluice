"""Contract model and YAML loader.

A contract is the single source of truth for a table's shape: column names and order, logical
dtypes, nullability, units, enums, numeric ranges, primary key and foreign keys.
"""

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, get_args

import polars as pl
import yaml

Dtype = Literal["int64", "int32", "decimal", "string", "date", "timestamp_utc"]

DTYPES: tuple[str, ...] = get_args(Dtype)
MONEY_PRECISION = 15
MONEY_SCALE = 2

_POLARS_DTYPES: dict[str, pl.DataType] = {
    "int64": pl.Int64(),
    "int32": pl.Int32(),
    "decimal": pl.Decimal(MONEY_PRECISION, MONEY_SCALE),
    "string": pl.String(),
    "date": pl.Date(),
    "timestamp_utc": pl.Datetime("us", "UTC"),
}

CONTRACTS_DIR = Path(__file__).resolve().parents[3] / "contracts"
_SAFE_PART = re.compile(r"^[a-z0-9_]+$")


@dataclass(frozen=True, slots=True)
class ColumnSpec:
    """One column of a contract."""

    name: str
    dtype: Dtype
    nullable: bool = False
    unit: str | None = None
    enum: list[str] | None = None
    min: float | None = None
    max: float | None = None

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("column name must be non-empty")
        if self.dtype not in DTYPES:
            raise ValueError(f"column {self.name!r}: unknown dtype {self.dtype!r}")
        if self.enum is not None and self.dtype != "string":
            raise ValueError(f"column {self.name!r}: enum is only valid on string columns")
        if (self.min is not None or self.max is not None) and self.dtype not in (
            "int64",
            "int32",
            "decimal",
        ):
            raise ValueError(f"column {self.name!r}: min/max are only valid on numeric columns")
        if self.min is not None and self.max is not None and self.min > self.max:
            raise ValueError(f"column {self.name!r}: min {self.min} > max {self.max}")


@dataclass(frozen=True, slots=True)
class Contract:
    """A versioned table contract.

    ``foreign_keys`` maps a local column to ``(table, column)`` in the referenced table.
    """

    name: str
    version: str
    primary_key: tuple[str, ...]
    columns: tuple[ColumnSpec, ...]
    foreign_keys: dict[str, tuple[str, str]] = field(default_factory=dict)
    locale: str = "en_IN"

    def __post_init__(self) -> None:
        names = [c.name for c in self.columns]
        if len(set(names)) != len(names):
            raise ValueError(f"contract {self.name!r}: duplicate column names")
        known = set(names)
        if not self.primary_key:
            raise ValueError(f"contract {self.name!r}: primary key must be non-empty")
        if missing := [k for k in self.primary_key if k not in known]:
            raise ValueError(f"contract {self.name!r}: primary key columns not declared: {missing}")
        if missing := [k for k in self.foreign_keys if k not in known]:
            raise ValueError(f"contract {self.name!r}: foreign key columns not declared: {missing}")

    def column(self, name: str) -> ColumnSpec:
        """Return the spec for column ``name``; raises ``KeyError`` if not declared."""
        for col in self.columns:
            if col.name == name:
                return col
        raise KeyError(f"contract {self.name!r} has no column {name!r}")

    def polars_schema(self) -> dict[str, pl.DataType]:
        """Column name -> Polars dtype, in contract order (``decimal`` is ``Decimal(15, 2)``)."""
        return {c.name: _POLARS_DTYPES[c.dtype] for c in self.columns}


def _parse_fk(table: str, raw: dict[str, Any]) -> dict[str, tuple[str, str]]:
    out: dict[str, tuple[str, str]] = {}
    for col, target in raw.items():
        ref_table, sep, ref_col = str(target).partition(".")
        if not sep or not ref_table or not ref_col:
            raise ValueError(f"contract {table!r}: foreign key {col!r} must be 'table.column'")
        out[str(col)] = (ref_table, ref_col)
    return out


def _parse_contract(raw: dict[str, Any]) -> Contract:
    name = raw["name"]
    return Contract(
        name=name,
        version=raw["version"],
        primary_key=tuple(raw["primary_key"]),
        columns=tuple(ColumnSpec(**col) for col in raw["columns"]),
        foreign_keys=_parse_fk(name, raw.get("foreign_keys") or {}),
        locale=raw["locale"],
    )


def load_contract(name: str, version: str = "v1", contracts_dir: Path | None = None) -> Contract:
    """Load ``<contracts_dir>/<name>.<version>.yaml`` (default dir: ``backend/contracts``).

    Raises:
        ValueError: If ``name``/``version`` are not ``[a-z0-9_]+`` or the file is inconsistent.
        FileNotFoundError: If the contract file does not exist.
    """
    for part in (name, version):
        if not _SAFE_PART.fullmatch(part):
            raise ValueError(f"invalid contract name/version: {part!r}")
    path = (contracts_dir or CONTRACTS_DIR) / f"{name}.{version}.yaml"
    with path.open(encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    contract = _parse_contract(raw)
    if (contract.name, contract.version) != (name, version):
        raise ValueError(f"contract file {path.name} declares {contract.name}.{contract.version}")
    return contract
