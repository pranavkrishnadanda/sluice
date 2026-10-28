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


# --- fix round 1: namespace race, lock timeouts -------------------------------------------


class _FakeCatalog:
    def __init__(self, exc: Exception | None, exists: bool) -> None:
        self.exc = exc
        self.exists = exists
        self.created: list[str] = []

    def create_namespace_if_not_exists(self, namespace: str) -> None:
        self.created.append(namespace)
        if self.exc is not None:
            raise self.exc

    def namespace_exists(self, namespace: str) -> bool:
        return self.exists


def _integrity_error() -> Exception:
    from sqlalchemy.exc import IntegrityError

    return IntegrityError("INSERT ...", {}, Exception("duplicate key"))


def test_ensure_namespace_creates_when_missing() -> None:
    from sluice.load.iceberg import ensure_namespace

    cat = _FakeCatalog(None, exists=False)
    ensure_namespace(cat, "sluice")  # type: ignore[arg-type]
    assert cat.created == ["sluice"]


def test_ensure_namespace_tolerates_concurrent_create() -> None:
    from sluice.load.iceberg import ensure_namespace

    ensure_namespace(_FakeCatalog(_integrity_error(), exists=True), "sluice")  # type: ignore[arg-type]


def test_ensure_namespace_reraises_when_namespace_still_missing() -> None:
    from sqlalchemy.exc import IntegrityError

    from sluice.load.iceberg import ensure_namespace

    with pytest.raises(IntegrityError):
        ensure_namespace(_FakeCatalog(_integrity_error(), exists=False), "sluice")  # type: ignore[arg-type]


class _FakeConn:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    def execute(self, sql: str, params: tuple[object, ...] = ()) -> None:
        self.calls.append((sql, params))


def test_set_lock_timeouts_sets_transaction_local_lock_and_statement_timeouts() -> None:
    from sluice.load.iceberg import LOCK_TIMEOUT, LOCK_WAIT_STATEMENT_TIMEOUT, set_lock_timeouts

    conn = _FakeConn()
    set_lock_timeouts(conn)  # type: ignore[arg-type]
    params = [p for _, p in conn.calls]
    assert ("lock_timeout", LOCK_TIMEOUT) in params
    assert ("statement_timeout", LOCK_WAIT_STATEMENT_TIMEOUT) in params
    assert all("set_config(%s, %s, true)" in sql for sql, _ in conn.calls)
    assert LOCK_TIMEOUT == "10s"


def test_restore_statement_timeout_after_locks() -> None:
    from sluice.load.iceberg import STATEMENT_TIMEOUT, restore_statement_timeout

    conn = _FakeConn()
    restore_statement_timeout(conn)  # type: ignore[arg-type]
    assert conn.calls == [
        ("SELECT set_config(%s, %s, true)", ("statement_timeout", STATEMENT_TIMEOUT))
    ]


def test_advisory_key_differs_per_table() -> None:
    assert advisory_key("sluice.orders", SHA, "v1") != advisory_key("sluice.lineitem", SHA, "v1")


# --- final review C-1 / Ruling F1: the run status the loader records -------------------------


def _output(*, quarantined: set[tuple[int, ...]] | None = None, whole: bool = False) -> object:
    import polars as pl

    from sluice.contracts.model import load_contract
    from sluice.evals.types import SystemOutput

    return SystemOutput(
        df=pl.DataFrame(schema=load_contract("orders").polars_schema()),
        flags=set(),
        quarantined=quarantined or set(),
        file_quarantined=whole,
        cost_usd=0.0,
        latency_s=0.0,
        meta={},
    )


def test_load_status_defaults_to_quarantined_when_rows_are_quarantined() -> None:
    from sluice.load.iceberg import load_status

    assert load_status(_output(quarantined={(1,)}), None) == "quarantined"  # type: ignore[arg-type]
    assert load_status(_output(whole=True), None) == "quarantined"  # type: ignore[arg-type]
    assert load_status(_output(), None) == "succeeded"  # type: ignore[arg-type]


def test_load_status_uses_the_pipeline_status() -> None:
    from sluice.load.iceberg import load_status

    assert load_status(_output(), "quarantined") == "quarantined"  # type: ignore[arg-type]


@pytest.mark.parametrize("status", ["failed", "rolled_back", "bogus"])
def test_load_status_refuses_a_status_that_must_not_load(status: str) -> None:
    from sluice.load.iceberg import LoadError, load_status

    with pytest.raises(LoadError, match="status"):
        load_status(_output(), status)  # type: ignore[arg-type]


def test_load_output_refuses_failed_run_before_any_io() -> None:
    from sluice.contracts.model import load_contract
    from sluice.load.iceberg import LoadError, load_output

    with pytest.raises(LoadError, match="status"):
        load_output(
            _output(),  # type: ignore[arg-type]
            load_contract("orders"),
            run_id="r1",
            file_sha256=SHA,
            settings=object(),  # type: ignore[arg-type]  # never used: refused first
            status="failed",
        )
