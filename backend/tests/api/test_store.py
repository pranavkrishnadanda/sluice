"""Run store and blob store: the parts that need no services.

Everything that needs Postgres or RustFS (claiming, event order, duplicate ``seq``, the S3 round
trip) is in ``tests/integration/test_api_store.py``. Here: argument validation (which must fail
before any connection is opened), event (de)serialisation for every contract event type, and
``LocalBlobStore`` end to end on a temporary directory.
"""

import asyncio
from collections.abc import AsyncIterator, Iterable
from pathlib import Path
from typing import Any

import pytest

from sluice.agent import events as ev
from sluice.api.blobs import BlobStore, InvalidKey, LocalBlobStore, TooLarge
from sluice.api.store import (
    TERMINAL_STATUSES,
    RunStore,
    dump_event,
    iso_utc,
    parse_event,
)

# Port 9 (discard) on loopback: nothing listens, so any accidental connection fails fast with
# ``psycopg.OperationalError`` instead of the ``ValueError`` the validation tests expect.
UNREACHABLE_DSN = "postgresql://nobody:nothing@127.0.0.1:9/none?connect_timeout=1"
SHA = "a" * 64


@pytest.fixture
def store() -> RunStore:
    return RunStore(UNREACHABLE_DSN)


# --------------------------------------------------------------------------------------------
# RunStore: validation happens before any I/O
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("status", ["queued", "running", "done", ""])
def test_finish_non_terminal_status_raises_value_error(store: RunStore, status: str) -> None:
    with pytest.raises(ValueError, match="terminal"):
        store.finish("run-1", status, 0.0)  # type: ignore[arg-type]


def test_terminal_statuses_are_the_three_final_statuses() -> None:
    assert frozenset({"succeeded", "quarantined", "failed"}) == TERMINAL_STATUSES


@pytest.mark.parametrize("cost", [-0.01, float("nan"), float("inf")])
def test_finish_invalid_cost_raises_value_error(store: RunStore, cost: float) -> None:
    with pytest.raises(ValueError):
        store.finish("run-1", "succeeded", cost)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"run_id": "../etc"},
        {"run_id": ""},
        {"run_id": "x" * 65},
        {"table": "customers"},
        {"file_sha256": "abc"},
        {"file_sha256": "G" * 64},
        {"file_name": ""},
        {"file_name": "x" * 256},
        {"blob_key": ""},
    ],
)
def test_create_run_invalid_arguments_raise_value_error(
    store: RunStore, kwargs: dict[str, Any]
) -> None:
    args: dict[str, Any] = {
        "run_id": "run-1",
        "file_name": "orders.csv",
        "table": "orders",
        "blob_key": "uploads/run-1/input.csv",
        "file_sha256": SHA,
    } | kwargs
    with pytest.raises(ValueError):
        store.create_run(**args)


@pytest.mark.parametrize("limit", [0, -1, 1001])
def test_list_runs_limit_out_of_range_raises_value_error(store: RunStore, limit: int) -> None:
    with pytest.raises(ValueError, match="limit"):
        store.list_runs(limit)


def test_append_event_unstamped_raises_value_error(store: RunStore) -> None:
    unstamped = ev.RulesApplied(actions=[])
    with pytest.raises(ValueError, match="seq"):
        store.append_event("run-1", unstamped)


def test_events_after_negative_seq_raises_value_error(store: RunStore) -> None:
    with pytest.raises(ValueError, match="after_seq"):
        store.events_after("run-1", -1)


@pytest.mark.parametrize("kwargs", [{"rows": -1}, {"cost_usd": -1.0}, {"cost_usd": float("nan")}])
def test_update_progress_invalid_values_raise_value_error(
    store: RunStore, kwargs: dict[str, Any]
) -> None:
    with pytest.raises(ValueError):
        store.update_progress("run-1", **kwargs)


def test_update_progress_nothing_to_update_is_a_no_op(store: RunStore) -> None:
    store.update_progress("run-1")  # no connection is attempted (it would fail)


# --------------------------------------------------------------------------------------------
# Events: every contract type survives the jsonb round trip
# --------------------------------------------------------------------------------------------

AT = "2026-10-27T10:14:03.000Z"


def _every_event() -> list[ev.EventModel]:
    node = ev.PlanNode(id="n1", title="fix", columns=["a"], tier="small", depends_on=[])
    built: list[ev.EventModel] = [
        ev.RunStarted(run_id="run-1", file_name="o.csv", table="orders", rows=None),
        ev.Profiled(issues=[ev.Issue(column="a", kind="rename", detail="d")]),
        ev.RulesApplied(actions=["trim"]),
        ev.PlanCreated(nodes=[node]),
        ev.NodeStarted(node_id="n1", attempt=1, max_attempts=3),
        ev.AttemptFailed(
            node_id="n1", attempt=1, max_attempts=3, error="e", tokens=5, cost_usd=0.0
        ),
        ev.NodeFinished(
            node_id="n1",
            attempt=2,
            tier="strong",
            tokens=10,
            cost_usd=0.25,
            latency_ms=40,
            transform_id="t1",
        ),
        ev.Verified(checks_passed=3, checks_total=3),
        ev.Loaded(rows_loaded=10, rows_quarantined=0, snapshot_id="123"),
        ev.RunFinished(status="succeeded", summary="ok", cost_usd=0, tokens=0),
    ]
    return [e.model_copy(update={"seq": i, "at": AT}) for i, e in enumerate(built, start=1)]


def test_every_event_type_is_covered() -> None:
    types = {e.type for e in _every_event()}  # type: ignore[attr-defined]
    assert len(types) == 10


@pytest.mark.parametrize("event", _every_event(), ids=lambda e: e.type)
def test_parse_event_round_trips_every_contract_type(event: ev.EventModel) -> None:
    payload = dump_event(event)
    assert parse_event(payload) == event


def test_dump_event_uses_contract_camel_case_keys() -> None:
    event = _every_event()[0]
    assert '"runId":"run-1"' in dump_event(event)


def test_iso_utc_is_millisecond_z() -> None:
    from datetime import datetime, timedelta, timezone

    moment = datetime(2026, 10, 27, 12, 14, 3, 123456, tzinfo=timezone(timedelta(hours=2)))
    assert iso_utc(moment) == "2026-10-27T10:14:03.123Z"


# --------------------------------------------------------------------------------------------
# LocalBlobStore
# --------------------------------------------------------------------------------------------


async def _chunks(parts: Iterable[bytes]) -> AsyncIterator[bytes]:
    for part in parts:
        yield part


class _CountingStream:
    """An async chunk source that records how many chunks were pulled."""

    def __init__(self, chunk: bytes, count: int) -> None:
        self.chunk = chunk
        self.count = count
        self.pulled = 0

    def __aiter__(self) -> _CountingStream:
        return self

    async def __anext__(self) -> bytes:
        if self.pulled >= self.count:
            raise StopAsyncIteration
        self.pulled += 1
        return self.chunk


def test_local_blob_store_satisfies_protocol(tmp_path: Path) -> None:
    assert isinstance(LocalBlobStore(tmp_path), BlobStore)


def test_local_put_stream_round_trips_and_returns_size(tmp_path: Path) -> None:
    blobs = LocalBlobStore(tmp_path)
    data = [b"a" * 1000, b"b" * 24]
    size = asyncio.run(blobs.put_stream("uploads/r1/input.csv", _chunks(data), max_bytes=1024))
    assert size == 1024
    assert blobs.get_bytes("uploads/r1/input.csv") == b"".join(data)


def test_local_put_stream_over_limit_raises_and_leaves_no_file(tmp_path: Path) -> None:
    blobs = LocalBlobStore(tmp_path)
    stream = _CountingStream(b"x" * 100, count=50)
    with pytest.raises(TooLarge):
        asyncio.run(blobs.put_stream("uploads/r1/input.csv", stream, max_bytes=250))
    assert stream.pulled == 3  # at most limit + one chunk was read
    assert [p for p in tmp_path.rglob("*") if p.is_file()] == []


def test_local_put_stream_source_error_leaves_no_file(tmp_path: Path) -> None:
    async def broken() -> AsyncIterator[bytes]:
        yield b"partial"
        raise ConnectionResetError("client went away")

    blobs = LocalBlobStore(tmp_path)
    with pytest.raises(ConnectionResetError):
        asyncio.run(blobs.put_stream("k/input.csv", broken(), max_bytes=1024))
    assert [p for p in tmp_path.rglob("*") if p.is_file()] == []


def test_local_put_stream_exactly_at_limit_is_accepted(tmp_path: Path) -> None:
    blobs = LocalBlobStore(tmp_path)
    size = asyncio.run(blobs.put_stream("k", _chunks([b"x" * 10]), max_bytes=10))
    assert size == 10


def test_local_open_local_yields_copy_with_suffix_and_removes_it(tmp_path: Path) -> None:
    blobs = LocalBlobStore(tmp_path / "root")
    blobs.put_bytes("uploads/r1/input.parquet", b"PAR1")
    with blobs.open_local("uploads/r1/input.parquet") as local:
        assert local.suffix == ".parquet"
        assert local.read_bytes() == b"PAR1"
        assert not local.is_relative_to(tmp_path / "root")
    assert not local.exists()


def test_local_open_local_missing_key_raises_file_not_found(tmp_path: Path) -> None:
    blobs = LocalBlobStore(tmp_path)
    with pytest.raises(FileNotFoundError), blobs.open_local("nope.csv"):
        pass


def test_local_delete_removes_and_tolerates_missing(tmp_path: Path) -> None:
    blobs = LocalBlobStore(tmp_path)
    blobs.put_bytes("a/b.bin", b"1")
    blobs.delete("a/b.bin")
    blobs.delete("a/b.bin")
    with pytest.raises(FileNotFoundError):
        blobs.get_bytes("a/b.bin")


@pytest.mark.parametrize(
    "key",
    ["", "/etc/passwd", "../x", "a/../../x", "a//b", "a/./b", "a\\b", "a/b/", "a\x00b", "é"],
)
def test_local_invalid_keys_are_rejected(tmp_path: Path, key: str) -> None:
    blobs = LocalBlobStore(tmp_path)
    with pytest.raises(InvalidKey):
        blobs.put_bytes(key, b"x")


def test_put_stream_rejects_non_positive_limit(tmp_path: Path) -> None:
    blobs = LocalBlobStore(tmp_path)
    with pytest.raises(ValueError, match="max_bytes"):
        asyncio.run(blobs.put_stream("k", _chunks([]), max_bytes=0))
