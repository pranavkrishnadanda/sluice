"""Pipeline events, shaped exactly like ``RunEvent`` in ``docs/api/contract.md``.

Field names are snake_case in Python and camelCase on the wire (``costUsd``, ``maxAttempts``,
``runId``, ...). Serialise with :meth:`EventModel.to_json` / :meth:`EventModel.to_dict`, or
``model_dump(by_alias=True)``. Parse any event with :data:`PIPELINE_EVENT_ADAPTER`, which
discriminates on ``type``.

Build an event without ``seq``/``at`` and hand it to :meth:`EventLog.emit`, which stamps both.
"""

import threading
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator, model_validator
from pydantic.alias_generators import to_camel

from sluice.agent.caps import MAX_ERROR_CHARS

TableName = Literal["orders", "lineitem"]
ModelTier = Literal["rules", "small", "strong"]
FinalStatus = Literal["succeeded", "quarantined", "failed"]
IssueKind = Literal[
    "rename", "type", "unit", "timezone", "date_format", "format", "encoding",
    "whitespace", "case", "destroyed", "duplicates", "foreign_key",
    "missing_column", "extra_column",
]  # fmt: skip

OpaqueId = Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")]
"""Run, node, transform and snapshot ids: opaque, ``[A-Za-z0-9_-]{1,64}``."""

Count = Annotated[int, Field(ge=0)]
Attempt = Annotated[int, Field(ge=1)]
Usd = Annotated[float, Field(ge=0, allow_inf_nan=False)]


class ContractModel(BaseModel):
    """Base for every wire model: camelCase aliases, strict types, frozen, no extra fields."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        serialize_by_alias=True,
        strict=True,
        extra="forbid",
        frozen=True,
    )

    def to_dict(self) -> dict[str, Any]:
        """The JSON-ready payload with contract (camelCase) keys."""
        return self.model_dump(mode="json", by_alias=True)

    def to_json(self) -> str:
        """The compact JSON payload with contract (camelCase) keys."""
        return self.model_dump_json(by_alias=True)


class Issue(ContractModel):
    """One problem found while profiling a column."""

    column: str
    kind: IssueKind
    detail: str


class PlanNode(ContractModel):
    """One step of the repair plan."""

    id: OpaqueId
    title: str
    columns: list[str]
    tier: ModelTier
    depends_on: list[OpaqueId]


class EventModel(ContractModel):
    """``EventBase``: ``seq`` and ``at`` are unset (``0`` / ``""``) until :meth:`EventLog.emit`."""

    seq: Count = 0
    at: str = ""


class _Attempted(EventModel):
    @model_validator(mode="after")
    def _attempt_within_max(self) -> Self:
        if self.attempt > self.max_attempts:  # type: ignore[attr-defined]
            raise ValueError("attempt must not exceed maxAttempts")
        return self


class RunStarted(EventModel):
    type: Literal["run_started"] = "run_started"
    run_id: OpaqueId
    file_name: str
    table: TableName
    rows: Count | None
    """``None`` (JSON ``null``) while the file has not been counted."""


class Profiled(EventModel):
    type: Literal["profiled"] = "profiled"
    issues: list[Issue]


class RulesApplied(EventModel):
    type: Literal["rules_applied"] = "rules_applied"
    actions: list[str]


class PlanCreated(EventModel):
    type: Literal["plan_created"] = "plan_created"
    nodes: list[PlanNode]
    """Empty when deterministic rules handled everything and no model is called."""


class NodeStarted(_Attempted):
    type: Literal["node_started"] = "node_started"
    node_id: OpaqueId
    attempt: Attempt
    max_attempts: Attempt


class AttemptFailed(_Attempted):
    type: Literal["attempt_failed"] = "attempt_failed"
    node_id: OpaqueId
    attempt: Attempt
    max_attempts: Attempt
    error: str
    """Truncated to :data:`~sluice.agent.caps.MAX_ERROR_CHARS` on construction."""
    tokens: Count
    cost_usd: Usd
    """Spend of this failed attempt."""

    @field_validator("error", mode="after")
    @classmethod
    def _truncate_error(cls, value: str) -> str:
        return value[:MAX_ERROR_CHARS]


class NodeFinished(EventModel):
    type: Literal["node_finished"] = "node_finished"
    node_id: OpaqueId
    attempt: Attempt
    tier: ModelTier
    """The tier that produced the accepted transform (may exceed the planned tier)."""
    tokens: Count
    cost_usd: Usd
    latency_ms: Count
    transform_id: OpaqueId


class Verified(EventModel):
    type: Literal["verified"] = "verified"
    checks_passed: Count
    checks_total: Count

    @model_validator(mode="after")
    def _passed_within_total(self) -> Self:
        if self.checks_passed > self.checks_total:
            raise ValueError("checksPassed must not exceed checksTotal")
        return self


class Loaded(EventModel):
    type: Literal["loaded"] = "loaded"
    rows_loaded: Count
    rows_quarantined: Count
    snapshot_id: OpaqueId


class RunFinished(EventModel):
    type: Literal["run_finished"] = "run_finished"
    status: FinalStatus
    summary: str
    cost_usd: Usd
    """Run total, failed attempts included."""
    tokens: Count
    """Run total, failed attempts included."""


PipelineEvent = Annotated[
    RunStarted | Profiled | RulesApplied | PlanCreated | NodeStarted | AttemptFailed
    | NodeFinished | Verified | Loaded | RunFinished,
    Field(discriminator="type"),
]  # fmt: skip
"""Any contract event, discriminated on ``type``."""

PIPELINE_EVENT_ADAPTER: TypeAdapter[PipelineEvent] = TypeAdapter(PipelineEvent)
"""Validates a payload (camelCase or snake_case keys) into the matching event model."""


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _iso_utc(moment: datetime) -> str:
    """``2026-10-27T10:14:03.000Z``: UTC, millisecond precision, ``Z`` suffix."""
    if moment.tzinfo is None:
        raise ValueError("clock must return a timezone-aware datetime")
    return moment.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class EventLog:
    """The ordered events of one run; ``seq`` starts at 1 and has no gaps.

    :meth:`emit` is guarded by a lock, so threads (and ``asyncio`` tasks) can share one log.
    Memory is O(events): a run emits a handful of events per plan node, which is bounded by
    ``MAX_ATTEMPTS`` times the plan size.

    Args:
        clock: Returns the current, timezone-aware time (stamped into ``at``).
        on_emit: Called with each stamped event, after it is recorded and outside the lock (so
            the callback may read :attr:`events` or emit into another log). Used to relay a
            pipeline's events live (``sluice.runs.execute``). Its exceptions propagate to the
            caller of :meth:`emit`. When threads share the log, callbacks may run in a different
            order than ``seq``.
    """

    __slots__ = ("_clock", "_events", "_lock", "_on_emit")

    def __init__(
        self,
        clock: Callable[[], datetime] = _utc_now,
        on_emit: Callable[[EventModel], None] | None = None,
    ) -> None:
        self._clock = clock
        self._events: list[EventModel] = []
        self._lock = threading.Lock()
        self._on_emit = on_emit

    def emit[E: EventModel](self, event: E) -> E:
        """Stamp ``event`` with the next ``seq`` and the current time, record and return it.

        The argument is not changed (events are frozen); the stamped copy is returned.
        Any ``seq``/``at`` already on the argument is overwritten.
        """
        with self._lock:
            stamped = event.model_copy(
                update={"seq": len(self._events) + 1, "at": _iso_utc(self._clock())}
            )
            self._events.append(stamped)
        if self._on_emit is not None:
            self._on_emit(stamped)
        return stamped

    @property
    def events(self) -> tuple[EventModel, ...]:
        """A snapshot of every emitted event, in ``seq`` order."""
        with self._lock:
            return tuple(self._events)
