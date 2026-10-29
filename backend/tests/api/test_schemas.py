"""Contract response models dump exactly the key sets of ``frontend/src/lib/api/types.ts``."""

import pytest
from pydantic import ValidationError

from sluice.api.schemas import (
    CI,
    DataPreview,
    ErrorBody,
    EvalSummary,
    ParetoPoint,
    ParetoSeries,
    PreviewCell,
    PreviewRow,
    RawInput,
    RunSummary,
    SystemMetrics,
    Transform,
)

AT = "2026-10-27T10:14:03.000Z"


def _ci() -> CI:
    return CI(mean=0.9, lo=0.85, hi=0.95)


def _run_summary() -> RunSummary:
    return RunSummary(
        id="run-1",
        file_name="orders.csv",
        table="orders",
        rows=None,
        status="queued",
        created_at=AT,
        cost_usd=0.0,
    )


def _preview_row() -> PreviewRow:
    return PreviewRow(
        pk="1|3",
        quarantined=False,
        reason=None,
        cells={"o_totalprice": PreviewCell(before="12,5", after="12.50", flagged=False)},
    )


def _eval_summary() -> EvalSummary:
    return EvalSummary(
        split="dev",
        n=300,
        generated_at=AT,
        systems=[
            SystemMetrics(
                system="sluice",
                cell_accuracy=_ci(),
                fabrication_rate=None,
                contract_pass_rate=_ci(),
                pass_hat4=_ci(),
                p95_latency_ms=_ci(),
                cost_per1k_rows_usd=_ci(),
            )
        ],
        pareto=[
            ParetoSeries(
                system="sluice",
                kind="router",
                points=[ParetoPoint(cost_per1k_rows_usd=0.02, quality=0.9, lambda_=0.5)],
            )
        ],
    )


def test_run_summary_dumps_contract_keys() -> None:
    dumped = _run_summary().model_dump(mode="json", by_alias=True)
    assert set(dumped) == {"id", "fileName", "table", "rows", "status", "createdAt", "costUsd"}


def test_transform_dumps_contract_keys() -> None:
    transform = Transform(
        id="t-1", node_id="n-1", attempt=1, language="polars", code="df", passed=True
    )
    dumped = transform.model_dump(mode="json", by_alias=True)
    assert set(dumped) == {"id", "nodeId", "attempt", "language", "code", "passed"}


def test_preview_cell_dumps_contract_keys() -> None:
    dumped = PreviewCell(before=None, after=None, flagged=True).model_dump(by_alias=True)
    assert set(dumped) == {"before", "after", "flagged"}


def test_preview_row_dumps_contract_keys() -> None:
    dumped = _preview_row().model_dump(mode="json", by_alias=True)
    assert set(dumped) == {"pk", "quarantined", "reason", "cells"}
    assert set(dumped["cells"]["o_totalprice"]) == {"before", "after", "flagged"}


def test_preview_row_cells_keep_target_column_names() -> None:
    # Cell keys are contract column names, not fields: the alias generator must not touch them.
    row = PreviewRow(
        pk="1",
        quarantined=False,
        reason=None,
        cells={"l_extended_price": PreviewCell(before="1", after="1", flagged=False)},
    )
    assert set(row.model_dump(by_alias=True)["cells"]) == {"l_extended_price"}


def test_data_preview_dumps_contract_keys() -> None:
    preview = DataPreview(
        columns=["o_orderkey"], rows=[_preview_row()], total_rows=1, next_offset=None
    )
    dumped = preview.model_dump(mode="json", by_alias=True)
    assert set(dumped) == {"columns", "rows", "totalRows", "nextOffset"}


def test_ci_dumps_contract_keys() -> None:
    assert set(_ci().model_dump(by_alias=True)) == {"mean", "lo", "hi"}


def test_eval_summary_dumps_contract_keys() -> None:
    dumped = _eval_summary().model_dump(mode="json", by_alias=True)
    assert set(dumped) == {"split", "n", "generatedAt", "systems", "pareto"}


def test_system_metrics_dumps_contract_keys() -> None:
    dumped = _eval_summary().model_dump(mode="json", by_alias=True)["systems"][0]
    assert set(dumped) == {
        "system",
        "cellAccuracy",
        "fabricationRate",
        "contractPassRate",
        "passHat4",
        "p95LatencyMs",
        "costPer1kRowsUsd",
    }


def test_pareto_series_and_point_dump_contract_keys() -> None:
    series = _eval_summary().model_dump(mode="json", by_alias=True)["pareto"][0]
    assert set(series) == {"system", "kind", "points"}
    assert series["points"][0] == {"costPer1kRowsUsd": 0.02, "quality": 0.9, "lambda": 0.5}


def test_models_serialise_by_alias_by_default() -> None:
    # FastAPI's response serialisation and plain model_dump() must both use camelCase.
    assert "fileName" in _run_summary().model_dump()


def test_models_accept_camel_case_input() -> None:
    summary = RunSummary.model_validate(_run_summary().model_dump(mode="json", by_alias=True))
    assert summary == _run_summary()


def test_pareto_point_accepts_lambda_alias() -> None:
    point = ParetoPoint.model_validate({"costPer1kRowsUsd": 0.1, "quality": 0.5, "lambda": None})
    assert point.lambda_ is None


def test_run_summary_rejects_unknown_table() -> None:
    with pytest.raises(ValidationError):
        RunSummary.model_validate({**_run_summary().model_dump(), "table": "customers"})


def test_run_summary_rejects_non_opaque_id() -> None:
    with pytest.raises(ValidationError):
        RunSummary.model_validate({**_run_summary().model_dump(), "id": "../etc"})


def test_run_summary_rejects_unknown_field() -> None:
    with pytest.raises(ValidationError):
        RunSummary.model_validate({**_run_summary().model_dump(), "extra": 1})


@pytest.mark.parametrize("value", [-0.1, 1.1])
def test_pareto_point_rejects_quality_outside_unit_interval(value: float) -> None:
    with pytest.raises(ValidationError):
        ParetoPoint(cost_per1k_rows_usd=0.1, quality=value, lambda_=None)


def test_error_body_omits_code_when_absent() -> None:
    assert ErrorBody(detail="Bad.").model_dump(exclude_none=True) == {"detail": "Bad."}


def test_raw_input_dumps_contract_keys_and_null_cells() -> None:
    page = RawInput(columns=["a", "b"], rows=[["1", None]], total_rows=3, next_offset=1)
    dumped = page.model_dump(mode="json", by_alias=True)
    assert dumped == {"columns": ["a", "b"], "rows": [["1", None]], "totalRows": 3,
                      "nextOffset": 1}  # fmt: skip
