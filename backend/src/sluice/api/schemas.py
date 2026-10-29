"""Response models mirroring ``frontend/src/lib/api/types.ts`` field for field.

Python fields are snake_case; the wire is camelCase (``alias_generator=to_camel``), and the
models serialise by alias by default so FastAPI's response encoding and ``model_dump()`` agree.
Run events are not redefined here: ``sluice.agent.events`` is already contract-shaped.
"""

from typing import Annotated, Final, Literal

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from sluice.agent.events import Count, OpaqueId, TableName, Usd

RunStatus = Literal["queued", "running", "succeeded", "quarantined", "failed"]
Fraction = Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]
Finite = Annotated[float, Field(allow_inf_nan=False)]

COST_PER_1K_ALIAS: Final = "costPer1kRowsUsd"
"""Explicit: ``to_camel("cost_per1k_rows_usd")`` gives ``costPer1KRowsUsd`` (capital K)."""


class ApiModel(BaseModel):
    """Base for every API response body: camelCase on the wire, frozen, no extra fields."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        serialize_by_alias=True,
        extra="forbid",
        frozen=True,
    )


class ErrorBody(ApiModel):
    """Every 4xx/5xx body: ``detail`` is written for the user, ``code`` is machine-readable."""

    detail: str
    code: str | None = None


class RunSummary(ApiModel):
    """One upload's run, as listed by ``GET /runs`` and returned by ``POST /runs``."""

    id: OpaqueId
    file_name: str
    table: TableName
    rows: Count | None
    status: RunStatus
    created_at: str
    cost_usd: Usd


class Transform(ApiModel):
    """The code one attempt produced; ``passed`` is false for failed attempts."""

    id: OpaqueId
    node_id: OpaqueId
    attempt: Annotated[int, Field(ge=1)]
    language: Literal["polars", "sql"]
    code: str
    passed: bool


class PreviewCell(ApiModel):
    """Raw source text before, canonical loaded value after; ``flagged`` = cell quarantine."""

    before: str | None
    after: str | None
    flagged: bool


class PreviewRow(ApiModel):
    """One row of the before/after preview. ``cells`` is keyed by target contract column."""

    pk: str
    quarantined: bool
    reason: str | None
    cells: dict[str, PreviewCell]


class DataPreview(ApiModel):
    """One page of ``GET /runs/{id}/preview``; ``next_offset`` is ``None`` after the last."""

    columns: list[str]
    rows: list[PreviewRow]
    total_rows: Count
    next_offset: Count | None


class RawInput(ApiModel):
    """One page of ``GET /runs/{id}/input``: the uploaded file's own columns and raw text."""

    columns: list[str]
    rows: list[list[str | None]]
    total_rows: Count
    next_offset: Count | None


class CI(ApiModel):
    """A 95% interval."""

    mean: Finite
    lo: Finite
    hi: Finite


class SystemMetrics(ApiModel):
    """One ``EvalSummary.systems[]`` row; ``None`` means "not measured", never zero."""

    system: str
    cell_accuracy: CI | None
    fabrication_rate: CI | None
    contract_pass_rate: CI | None
    pass_hat4: CI | None
    p95_latency_ms: CI | None
    cost_per1k_rows_usd: CI | None = Field(alias=COST_PER_1K_ALIAS)


class ParetoPoint(ApiModel):
    """One operating point; ``lambda_`` (wire ``lambda``) is ``None`` for baselines."""

    cost_per1k_rows_usd: Usd = Field(alias=COST_PER_1K_ALIAS)
    quality: Fraction
    lambda_: Finite | None = Field(alias="lambda")


class ParetoSeries(ApiModel):
    """A system's cost/quality frontier: one point for a baseline, one per lambda for a router."""

    system: str
    kind: Literal["baseline", "router"]
    points: list[ParetoPoint]


class EvalSummary(ApiModel):
    """``GET /evals/summary``: the latest scored eval run."""

    split: Literal["dev", "holdout", "ood"]
    n: Count
    generated_at: str
    systems: list[SystemMetrics]
    pareto: list[ParetoSeries]
