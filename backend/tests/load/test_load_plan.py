"""Unit tests (no services): the idempotency decision logic of the Iceberg load."""

import pytest

from sluice.load.iceberg import (
    PROP_CONTRACT_VERSION,
    PROP_FILE_SHA256,
    LoadPlan,
    advisory_key,
    committed_snapshot_id,
    plan_load,
)

SHA = "a" * 64
OTHER = "b" * 64


def test_plan_load_already_recorded_skips_everything() -> None:
    plan = plan_load(
        recorded=True, main_committed=False, quarantine_committed=False, n_rows=5, n_quarantined=2
    )
    assert plan == LoadPlan(skip=True, append_main=False, append_quarantine=False)


def test_plan_load_fresh_file_appends_both_tables() -> None:
    plan = plan_load(
        recorded=False, main_committed=False, quarantine_committed=False, n_rows=5, n_quarantined=2
    )
    assert plan == LoadPlan(skip=False, append_main=True, append_quarantine=True)


def test_plan_load_no_quarantined_rows_skips_quarantine_append() -> None:
    plan = plan_load(
        recorded=False, main_committed=False, quarantine_committed=False, n_rows=5, n_quarantined=0
    )
    assert plan == LoadPlan(skip=False, append_main=True, append_quarantine=False)


def test_plan_load_empty_output_appends_nothing_but_records() -> None:
    plan = plan_load(
        recorded=False, main_committed=False, quarantine_committed=False, n_rows=0, n_quarantined=0
    )
    assert plan == LoadPlan(skip=False, append_main=False, append_quarantine=False)


def test_plan_load_crash_after_main_commit_does_not_append_main_again() -> None:
    plan = plan_load(
        recorded=False, main_committed=True, quarantine_committed=False, n_rows=5, n_quarantined=2
    )
    assert plan == LoadPlan(skip=False, append_main=False, append_quarantine=True, recovering=True)


def test_plan_load_crash_after_both_commits_only_records() -> None:
    plan = plan_load(
        recorded=False, main_committed=True, quarantine_committed=True, n_rows=5, n_quarantined=2
    )
    assert plan == LoadPlan(skip=False, append_main=False, append_quarantine=False, recovering=True)


def test_plan_load_fresh_is_not_recovering() -> None:
    plan = plan_load(
        recorded=False, main_committed=False, quarantine_committed=False, n_rows=5, n_quarantined=0
    )
    assert plan.recovering is False


def _summary(sha: str, version: str = "v1") -> dict[str, str]:
    return {"operation": "append", PROP_FILE_SHA256: sha, PROP_CONTRACT_VERSION: version}


def test_committed_snapshot_id_finds_matching_snapshot() -> None:
    snaps = [(10, {"operation": "append"}), (11, _summary(SHA)), (12, _summary(OTHER))]
    assert committed_snapshot_id(snaps, SHA, "v1") == 11


def test_committed_snapshot_id_requires_matching_contract_version() -> None:
    assert committed_snapshot_id([(11, _summary(SHA, "v1"))], SHA, "v2") is None


def test_committed_snapshot_id_none_when_absent() -> None:
    assert committed_snapshot_id([(10, _summary(OTHER))], SHA, "v1") is None
    assert committed_snapshot_id([], SHA, "v1") is None


def test_advisory_key_is_deterministic_signed_bigint() -> None:
    key = advisory_key(SHA, "v1")
    assert key == advisory_key(SHA, "v1")
    assert -(2**63) <= key < 2**63


@pytest.mark.parametrize(("sha", "version"), [(OTHER, "v1"), (SHA, "v2")])
def test_advisory_key_differs_per_idempotency_key(sha: str, version: str) -> None:
    assert advisory_key(sha, version) != advisory_key(SHA, "v1")
