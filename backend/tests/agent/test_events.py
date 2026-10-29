"""Pipeline events must serialise exactly as ``RunEvent`` in ``docs/api/contract.md``."""

import json
from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from sluice.agent import caps
from sluice.agent.events import (
    PIPELINE_EVENT_ADAPTER,
    AttemptFailed,
    EventLog,
    Issue,
    Loaded,
    NodeFinished,
    NodeStarted,
    PlanCreated,
    PlanNode,
    Profiled,
    RulesApplied,
    RunFinished,
    RunStarted,
    Verified,
)


def _one_of_each() -> list:
    return [
        RunStarted(run_id="run-1", file_name="orders.csv", table="orders", rows=None),
        Profiled(issues=[Issue(column="amt", kind="rename", detail="amt -> o_totalprice")]),
        RulesApplied(actions=["trim whitespace"]),
        PlanCreated(
            nodes=[
                PlanNode(
                    id="n1", title="Fix dates", columns=["o_orderdate"], tier="small", depends_on=[]
                )
            ]
        ),
        NodeStarted(node_id="n1", attempt=1, max_attempts=3),
        AttemptFailed(
            node_id="n1", attempt=1, max_attempts=3, error="boom", tokens=120, cost_usd=0.001
        ),
        NodeFinished(
            node_id="n1",
            attempt=2,
            tier="strong",
            tokens=300,
            cost_usd=0.002,
            latency_ms=850,
            transform_id="t-1",
        ),
        Verified(checks_passed=9, checks_total=9),
        Loaded(rows_loaded=100, rows_quarantined=0, snapshot_id="snap-1"),
        RunFinished(status="succeeded", summary="ok", cost_usd=0.003, tokens=420),
    ]


# The exact key sets from the contract's RunEvent union (EventBase + variant).
CONTRACT_KEYS = {
    "run_started": {"seq", "at", "type", "runId", "fileName", "table", "rows"},
    "profiled": {"seq", "at", "type", "issues"},
    "rules_applied": {"seq", "at", "type", "actions"},
    "plan_created": {"seq", "at", "type", "nodes"},
    "node_started": {"seq", "at", "type", "nodeId", "attempt", "maxAttempts"},
    "attempt_failed": {
        "seq", "at", "type", "nodeId", "attempt", "maxAttempts", "error", "tokens", "costUsd",
    },
    "node_finished": {
        "seq", "at", "type", "nodeId", "attempt", "tier", "tokens", "costUsd", "latencyMs",
        "transformId",
    },
    "verified": {"seq", "at", "type", "checksPassed", "checksTotal"},
    "loaded": {"seq", "at", "type", "rowsLoaded", "rowsQuarantined", "snapshotId"},
    "run_finished": {"seq", "at", "type", "status", "summary", "costUsd", "tokens"},
}  # fmt: skip


def _emitted() -> list[dict]:
    log = EventLog()
    for event in _one_of_each():
        log.emit(event)
    return [json.loads(e.to_json()) for e in log.events]


def test_caps_values_match_the_plan() -> None:
    assert caps.MAX_ATTEMPTS == 3
    assert caps.MAX_TOKENS_PER_FILE == 60_000
    assert caps.MAX_WALL_S_PER_FILE == 180
    assert caps.SANDBOX_TIMEOUT_S == 20
    assert caps.SANDBOX_MEM_MB == 512
    assert caps.SANDBOX_PIDS == 64
    assert caps.MAX_ERROR_CHARS == 2_000


def test_events_json_keys_match_contract_exactly() -> None:
    payloads = _emitted()
    assert [p["type"] for p in payloads] == list(CONTRACT_KEYS)
    for payload in payloads:
        assert set(payload) == CONTRACT_KEYS[payload["type"]], payload["type"]


def test_events_json_puts_seq_at_type_first() -> None:
    for payload in _emitted():
        assert list(payload)[:3] == ["seq", "at", "type"]


def test_nested_models_serialise_camel_case() -> None:
    payloads = {p["type"]: p for p in _emitted()}
    assert payloads["plan_created"]["nodes"] == [
        {"id": "n1", "title": "Fix dates", "columns": ["o_orderdate"], "tier": "small",
         "dependsOn": []}
    ]  # fmt: skip
    assert payloads["profiled"]["issues"] == [
        {"column": "amt", "kind": "rename", "detail": "amt -> o_totalprice"}
    ]


def test_run_started_rows_null_serialises_as_null() -> None:
    payload = _emitted()[0]
    assert "rows" in payload
    assert payload["rows"] is None


def test_event_log_seq_strictly_increasing_from_one() -> None:
    log = EventLog()
    for event in _one_of_each():
        log.emit(event)
    assert [e.seq for e in log.events] == list(range(1, 11))


def test_event_log_seq_is_per_log() -> None:
    first, second = EventLog(), EventLog()
    first.emit(Verified(checks_passed=1, checks_total=1))
    first.emit(Verified(checks_passed=1, checks_total=1))
    stamped = second.emit(Verified(checks_passed=1, checks_total=1))
    assert stamped.seq == 1


def test_event_log_emit_returns_stamped_event_and_keeps_input_untouched() -> None:
    log = EventLog()
    raw = Verified(checks_passed=1, checks_total=2)
    stamped = log.emit(raw)
    assert stamped.seq == 1
    assert stamped.checks_total == 2
    assert raw.seq == 0
    assert log.events == (stamped,)


def test_event_log_events_is_read_only_snapshot() -> None:
    log = EventLog()
    log.emit(Verified(checks_passed=1, checks_total=1))
    snapshot = log.events
    log.emit(Verified(checks_passed=1, checks_total=1))
    assert len(snapshot) == 1
    assert len(log.events) == 2


def test_event_log_at_is_iso_utc_with_millis_and_z() -> None:
    fixed = datetime(2026, 10, 27, 10, 14, 3, 910_123, tzinfo=UTC)
    log = EventLog(clock=lambda: fixed)
    stamped = log.emit(Verified(checks_passed=1, checks_total=1))
    assert stamped.at == "2026-10-27T10:14:03.910Z"


def test_event_log_at_converts_non_utc_clock_to_utc() -> None:
    plus_two = datetime(2026, 10, 27, 14, 0, 0, tzinfo=timezone(timedelta(hours=2)))
    log = EventLog(clock=lambda: plus_two)
    assert log.emit(Verified(checks_passed=1, checks_total=1)).at == "2026-10-27T12:00:00.000Z"


def test_event_log_default_clock_produces_parseable_utc() -> None:
    stamped = EventLog().emit(Verified(checks_passed=1, checks_total=1))
    parsed = datetime.fromisoformat(stamped.at)
    assert stamped.at.endswith("Z")
    assert parsed.utcoffset() == timedelta(0)


def test_attempt_failed_error_truncated_to_cap() -> None:
    event = AttemptFailed(
        node_id="n1", attempt=1, max_attempts=3, error="x" * 5_000, tokens=0, cost_usd=0.0
    )
    assert len(event.error) == caps.MAX_ERROR_CHARS


def test_attempt_failed_error_at_cap_kept_whole() -> None:
    error = "y" * caps.MAX_ERROR_CHARS
    event = AttemptFailed(
        node_id="n1", attempt=1, max_attempts=3, error=error, tokens=0, cost_usd=0.0
    )
    assert event.error == error


def test_attempt_failed_error_truncated_when_validated_from_json() -> None:
    payload = json.dumps(
        {"seq": 1, "at": "2026-10-27T10:14:03.000Z", "type": "attempt_failed", "nodeId": "n1",
         "attempt": 1, "maxAttempts": 3, "error": "z" * 2_001, "tokens": 1, "costUsd": 0.0}
    )  # fmt: skip
    event = PIPELINE_EVENT_ADAPTER.validate_json(payload)
    assert isinstance(event, AttemptFailed)
    assert len(event.error) == caps.MAX_ERROR_CHARS


@pytest.mark.parametrize("index", range(10))
def test_union_round_trips_every_variant(index: int) -> None:
    log = EventLog()
    for event in _one_of_each():
        log.emit(event)
    original = log.events[index]
    restored = PIPELINE_EVENT_ADAPTER.validate_json(original.to_json())
    assert type(restored) is type(original)
    assert restored == original


def test_union_rejects_unknown_type() -> None:
    with pytest.raises(ValidationError):
        PIPELINE_EVENT_ADAPTER.validate_python({"seq": 1, "at": "x", "type": "nope"})


def test_events_accept_snake_case_names_too() -> None:
    event = NodeStarted.model_validate({"node_id": "n1", "attempt": 1, "max_attempts": 3})
    assert event.max_attempts == 3


def test_events_are_frozen() -> None:
    event = Verified(checks_passed=1, checks_total=1)
    with pytest.raises(ValidationError):
        event.checks_passed = 2  # type: ignore[misc]


@pytest.mark.parametrize(
    "build",
    [
        lambda: RunStarted(run_id="bad id!", file_name="f.csv", table="orders", rows=None),
        lambda: RunStarted(run_id="r", file_name="f.csv", table="customers", rows=None),
        lambda: RunStarted(run_id="r", file_name="f.csv", table="orders", rows=-1),
        lambda: NodeStarted(node_id="n1", attempt=0, max_attempts=3),
        lambda: NodeStarted(node_id="n1", attempt=4, max_attempts=3),
        lambda: NodeStarted(node_id="x" * 65, attempt=1, max_attempts=3),
        lambda: AttemptFailed(
            node_id="n1", attempt=1, max_attempts=3, error="e", tokens=-1, cost_usd=0.0
        ),
        lambda: AttemptFailed(
            node_id="n1", attempt=1, max_attempts=3, error="e", tokens=1, cost_usd=float("nan")
        ),
        lambda: NodeFinished(
            node_id="n1", attempt=1, tier="huge", tokens=1, cost_usd=0.0, latency_ms=1,
            transform_id="t",
        ),
        lambda: Verified(checks_passed=3, checks_total=2),
        lambda: RunFinished(status="running", summary="", cost_usd=0.0, tokens=0),
        lambda: Issue(column="c", kind="vibes", detail=""),
        lambda: Verified(checks_passed=1, checks_total=1, extra_field=1),
        lambda: NodeStarted(node_id="n1", attempt="1", max_attempts=3),
    ],
)  # fmt: skip
def test_events_reject_invalid_input(build) -> None:
    with pytest.raises(ValidationError):
        build()


def test_event_log_on_emit_receives_each_stamped_event_in_order() -> None:
    seen: list[object] = []
    log = EventLog(on_emit=seen.append)
    first = log.emit(Verified(checks_passed=1, checks_total=1))
    second = log.emit(Verified(checks_passed=0, checks_total=1))
    assert seen == [first, second]
    assert [e.seq for e in seen] == [1, 2]  # type: ignore[attr-defined]


def test_event_log_on_emit_runs_outside_the_lock() -> None:
    log: EventLog

    def reenter(event: object) -> None:
        # Would deadlock if the callback held the (non-reentrant) lock.
        assert len(log.events) >= 1

    log = EventLog(on_emit=reenter)
    log.emit(Verified(checks_passed=1, checks_total=1))
