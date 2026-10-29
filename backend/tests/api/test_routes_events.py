"""``GET /runs/{id}/events``: replay, live tail, keep-alive, ``Last-Event-ID`` resume, and the
subscription released on disconnect or close (``docs/api/contract.md``, "Event stream (SSE)").

No services, no ports: an in-memory store, the real :class:`EventHub`, and the app driven over
ASGI. Streams that end on their own go through ``httpx.AsyncClient``; ``ASGITransport`` buffers
the whole body, so live streams (keep-alive, disconnect) use :class:`_Stream`, a minimal ASGI
client that yields chunks as they are sent and can disconnect on demand.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest
from api_fakes import FakeStore, finished, started
from fastapi import FastAPI

from sluice.agent import events as ev
from sluice.agent.events import PIPELINE_EVENT_ADAPTER, EventLog, EventModel
from sluice.api.app import create_app
from sluice.api.hub import MAX_QUEUED_EVENTS, EventHub
from sluice.api.settings import ApiSettings

KEEPALIVE_S = 0.05
KEEPALIVE = ": keep-alive"
UI_ORIGIN = "http://localhost:3100"


def _run(make: Callable[[], Awaitable[Any]]) -> Any:
    return asyncio.run(asyncio.wait_for(make(), timeout=10))


async def _until(predicate: Callable[[], bool], timeout_s: float = 5.0) -> None:
    deadline = time.monotonic() + timeout_s
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.005)


def _stamp(run_id: str, *events: EventModel) -> list[EventModel]:
    log = EventLog()
    return [log.emit(e) for e in events]


def _history(run_id: str, middle: int = 1) -> list[EventModel]:
    """``run_started``, ``middle`` x ``rules_applied``, ``run_finished``; seq 1..middle+2."""
    body = [ev.RulesApplied(actions=[]) for _ in range(middle)]
    return _stamp(run_id, started(run_id), *body, finished())


def _store_with(run_id: str, events: list[EventModel], status: str) -> FakeStore:
    store = FakeStore()
    store.add(run_id, status=status)
    for event in events:
        store.append_event(run_id, event)
    return store


@asynccontextmanager
async def _app(store: FakeStore) -> AsyncIterator[FastAPI]:
    app = create_app(
        settings=ApiSettings(keepalive_s=KEEPALIVE_S),
        store=store,  # type: ignore[arg-type]
        worker=False,
    )
    async with app.router.lifespan_context(app):
        yield app


def _hub(app: FastAPI) -> EventHub:
    hub = app.state.hub
    assert isinstance(hub, EventHub)
    return hub


def _messages(body: str) -> list[str]:
    """The stream split into messages (keep-alive comments included), separators dropped."""
    assert body == "" or body.endswith("\n\n"), body[-40:]
    return [m for m in body.split("\n\n") if m]


def _events(body: str) -> list[dict[str, Any]]:
    """Every data message as ``{"id": int, "data": parsed JSON}``; asserts the wire shape."""
    out: list[dict[str, Any]] = []
    for message in _messages(body):
        if message.startswith(":"):
            assert message == KEEPALIVE
            continue
        lines = message.split("\n")
        assert len(lines) == 2, message
        id_line, data_line = lines
        assert id_line.startswith("id: ") and data_line.startswith("data: "), message
        data = json.loads(data_line.removeprefix("data: "))
        assert int(id_line.removeprefix("id: ")) == data["seq"]
        out.append({"id": data["seq"], "data": data})
    return out


async def _get(app: FastAPI, path: str, headers: dict[str, str] | None = None) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://api.test") as client:
        return await client.get(path, headers=headers or {})


class _Stream:
    """One live ASGI request: :meth:`read_until` collects the body as it streams."""

    def __init__(self, app: FastAPI, path: str, headers: dict[str, str] | None = None) -> None:
        self.status: int | None = None
        self.headers: dict[str, str] = {}
        self.body = ""
        self.ended = False
        self._chunks: asyncio.Queue[str | None] = asyncio.Queue()
        self._disconnect = asyncio.Event()
        self._sent_request = False
        raw = [(b"host", b"api.test")]
        raw += [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "root_path": "",
            "headers": raw,
            "client": ("127.0.0.1", 50000),
            "server": ("api.test", 80),
        }
        self.task = asyncio.create_task(app(scope, self._receive, self._send))

    async def _receive(self) -> dict[str, Any]:
        if not self._sent_request:
            self._sent_request = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await self._disconnect.wait()
        return {"type": "http.disconnect"}

    async def _send(self, message: dict[str, Any]) -> None:
        if message["type"] == "http.response.start":
            self.status = message["status"]
            self.headers = {k.decode(): v.decode() for k, v in message["headers"]}
        elif message["type"] == "http.response.body":
            chunk = message.get("body", b"").decode()
            if chunk:
                await self._chunks.put(chunk)
            if not message.get("more_body", False):
                await self._chunks.put(None)

    async def read_until(self, predicate: Callable[[str], bool]) -> str:
        while not predicate(self.body):
            chunk = await self._chunks.get()
            if chunk is None:
                self.ended = True
                break
            self.body += chunk
        return self.body

    async def read_to_end(self) -> str:
        return await self.read_until(lambda _b: False)

    async def disconnect(self) -> None:
        self._disconnect.set()
        await asyncio.wait_for(self.task, timeout=5)


# ------------------------------------------------------------------------------------------
# Errors and headers


def test_events_unknown_run_returns_404_contract_body() -> None:
    async def go() -> httpx.Response:
        async with _app(FakeStore()) as app:
            return await _get(app, "/runs/nope/events", {"Origin": UI_ORIGIN})

    res = _run(go)

    assert res.status_code == 404
    assert res.headers["content-type"].startswith("application/json")
    assert res.json() == {"detail": "Run not found.", "code": "not_found"}
    assert res.headers["access-control-allow-origin"] == UI_ORIGIN  # error responses too


def test_events_invalid_run_id_returns_404() -> None:
    async def go() -> httpx.Response:
        async with _app(FakeStore()) as app:
            return await _get(app, "/runs/bad%20id%3B/events")

    assert _run(go).status_code == 404


def test_events_response_is_uncached_event_stream_with_cors() -> None:
    store = _store_with("run-a", _history("run-a"), "succeeded")

    async def go() -> httpx.Response:
        async with _app(store) as app:
            return await _get(app, "/runs/run-a/events", {"Origin": UI_ORIGIN})

    res = _run(go)

    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/event-stream")
    assert res.headers["cache-control"] == "no-cache"
    assert res.headers["x-accel-buffering"] == "no"
    assert res.headers["access-control-allow-origin"] == UI_ORIGIN


# ------------------------------------------------------------------------------------------
# Replay


def test_events_finished_run_replays_history_and_closes() -> None:
    history = _history("run-a", middle=2)
    store = _store_with("run-a", history, "succeeded")

    async def go() -> tuple[httpx.Response, int]:
        async with _app(store) as app:
            res = await _get(app, "/runs/run-a/events")
            return res, _hub(app).subscriber_count("run-a")

    res, subscribers = _run(go)
    events = _events(res.text)

    assert [e["id"] for e in events] == [1, 2, 3, 4]
    assert events[-1]["data"]["type"] == "run_finished"
    assert subscribers == 0


def test_events_messages_are_one_line_camel_case_contract_events() -> None:
    history = _history("run-a")
    store = _store_with("run-a", history, "succeeded")

    async def go() -> httpx.Response:
        async with _app(store) as app:
            return await _get(app, "/runs/run-a/events")

    events = _events(_run(go).text)

    assert [e["data"] for e in events] == [json.loads(e.to_json()) for e in history]
    for event in events:
        PIPELINE_EVENT_ADAPTER.validate_python(event["data"])
    assert "costUsd" in events[-1]["data"] and "runId" in events[0]["data"]


@pytest.mark.parametrize(("header", "first"), [("3", 4), ("0", 1), ("5", None)])
def test_events_last_event_id_resumes_after_that_seq(header: str, first: int | None) -> None:
    store = _store_with("run-a", _history("run-a", middle=3), "succeeded")  # seq 1..5

    async def go() -> httpx.Response:
        async with _app(store) as app:
            return await _get(app, "/runs/run-a/events", {"Last-Event-ID": header})

    events = _events(_run(go).text)

    if first is None:
        assert events == []
    else:
        assert events[0]["id"] == first and events[-1]["id"] == 5


@pytest.mark.parametrize("header", ["abc", "-1", "", "2.5", " ", "99999999999999999999999x"])
def test_events_invalid_last_event_id_replays_from_start(header: str) -> None:
    store = _store_with("run-a", _history("run-a"), "succeeded")

    async def go() -> httpx.Response:
        async with _app(store) as app:
            return await _get(app, "/runs/run-a/events", {"Last-Event-ID": header})

    assert [e["id"] for e in _events(_run(go).text)] == [1, 2, 3]


# ------------------------------------------------------------------------------------------
# Live tail


def _publish(store: FakeStore, hub: EventHub, run_id: str, event: EventModel) -> None:
    """What the worker does: store first, then publish."""
    store.append_event(run_id, event)
    hub.publish(run_id, event)


def test_events_running_run_replays_then_follows_live_until_run_finished() -> None:
    history = _history("run-a", middle=2)
    store = _store_with("run-a", history[:2], "running")

    async def go() -> tuple[str, bool, int]:
        async with _app(store) as app:
            hub = _hub(app)
            stream = _Stream(app, "/runs/run-a/events")
            await stream.read_until(lambda b: "id: 2\n" in b)
            await _until(lambda: hub.subscriber_count("run-a") == 1)
            for event in history[2:]:
                _publish(store, hub, "run-a", event)
            body = await stream.read_to_end()
            await asyncio.wait_for(stream.task, timeout=5)
            return body, stream.ended, hub.subscriber_count("run-a")

    body, ended, subscribers = _run(go)

    assert [e["id"] for e in _events(body)] == [1, 2, 3, 4]
    assert ended and subscribers == 0


def test_events_published_between_subscribe_and_replay_is_sent_exactly_once() -> None:
    history = _history("run-a")
    store = _store_with("run-a", history[:1], "running")
    holder: dict[str, EventHub] = {}
    real_events_after = store.events_after
    calls = 0

    def events_after(run_id: str, after_seq: int) -> list[EventModel]:
        # The stream has subscribed; event 2 is stored and published before the replay reads.
        nonlocal calls
        calls += 1
        if calls == 1:
            _publish(store, holder["hub"], run_id, history[1])
        return real_events_after(run_id, after_seq)

    store.events_after = events_after  # type: ignore[method-assign]

    async def go() -> str:
        async with _app(store) as app:
            holder["hub"] = hub = _hub(app)
            stream = _Stream(app, "/runs/run-a/events")
            await stream.read_until(lambda b: "id: 2\n" in b)
            _publish(store, hub, "run-a", history[2])
            body = await stream.read_to_end()
            await asyncio.wait_for(stream.task, timeout=5)
            return body

    assert [e["id"] for e in _events(_run(go))] == [1, 2, 3]


def test_events_queued_run_gets_keep_alive_before_first_event() -> None:
    history = _history("run-q")
    store = _store_with("run-q", [], "queued")

    async def go() -> str:
        async with _app(store) as app:
            hub = _hub(app)
            stream = _Stream(app, "/runs/run-q/events")
            await stream.read_until(lambda b: b.count(KEEPALIVE) >= 2)
            for event in history:
                _publish(store, hub, "run-q", event)
            body = await stream.read_to_end()
            await asyncio.wait_for(stream.task, timeout=5)
            return body

    body = _run(go)
    messages = _messages(body)

    assert messages[0] == KEEPALIVE and messages[1] == KEEPALIVE
    assert [e["id"] for e in _events(body)] == [1, 2, 3]


def test_events_disconnect_mid_stream_releases_subscription() -> None:
    store = _store_with("run-a", _history("run-a")[:1], "running")

    async def go() -> tuple[int, int]:
        async with _app(store) as app:
            hub = _hub(app)
            stream = _Stream(app, "/runs/run-a/events")
            await stream.read_until(lambda b: KEEPALIVE in b)
            during = hub.subscriber_count("run-a")
            await stream.disconnect()
            return during, hub.subscriber_count("run-a")

    during, after = _run(go)

    assert during == 1
    assert after == 0


# ------------------------------------------------------------------------------------------
# The subscription ends before run_finished arrived live


def test_events_hub_closed_after_terminal_stored_sends_it_from_the_store() -> None:
    history = _history("run-a")
    store = _store_with("run-a", history[:2], "running")

    async def go() -> tuple[str, int]:
        async with _app(store) as app:
            hub = _hub(app)
            stream = _Stream(app, "/runs/run-a/events")
            await stream.read_until(lambda b: "id: 2\n" in b)
            await _until(lambda: hub.subscriber_count("run-a") == 1)
            store.append_event("run-a", history[2])  # stored, never published live
            store.runs["run-a"] = store.runs["run-a"].model_copy(update={"status": "succeeded"})
            hub.close("run-a")
            body = await stream.read_to_end()
            await asyncio.wait_for(stream.task, timeout=5)
            return body, hub.subscriber_count("run-a")

    body, subscribers = _run(go)

    assert [e["id"] for e in _events(body)] == [1, 2, 3]
    assert _events(body)[-1]["data"]["type"] == "run_finished"
    assert subscribers == 0


def test_events_hub_closed_while_run_active_resubscribes_and_finishes() -> None:
    history = _history("run-a")
    store = _store_with("run-a", history[:1], "running")

    async def go() -> str:
        async with _app(store) as app:
            hub = _hub(app)
            stream = _Stream(app, "/runs/run-a/events")
            await stream.read_until(lambda b: "id: 1\n" in b)
            await _until(lambda: hub.subscriber_count("run-a") == 1)
            hub.close("run-a")
            seen = len(stream.body)
            # A keep-alive after the close comes from a fresh subscription's live loop.
            await stream.read_until(lambda b: KEEPALIVE in b[seen:])
            assert not stream.ended
            assert hub.subscriber_count("run-a") == 1
            for event in history[1:]:
                _publish(store, hub, "run-a", event)
            body = await stream.read_to_end()
            await asyncio.wait_for(stream.task, timeout=5)
            return body

    assert [e["id"] for e in _events(_run(go))] == [1, 2, 3]


def test_events_overflowed_subscriber_catches_up_from_store_without_gaps() -> None:
    total = MAX_QUEUED_EVENTS + 5
    history = _history("run-a", middle=total)  # seq 1..total+2
    store = _store_with("run-a", history[:1], "running")

    async def go() -> str:
        async with _app(store) as app:
            hub = _hub(app)
            stream = _Stream(app, "/runs/run-a/events")
            await stream.read_until(lambda b: "id: 1\n" in b)
            await _until(lambda: hub.subscriber_count("run-a") == 1)
            # A burst with no await in between: every delivery runs before the stream reads.
            for event in history[1:]:
                _publish(store, hub, "run-a", event)
            body = await stream.read_to_end()
            await asyncio.wait_for(stream.task, timeout=5)
            return body

    ids = [e["id"] for e in _events(_run(go))]

    assert ids == list(range(1, total + 3))


def test_events_finished_run_without_stored_terminal_closes_instead_of_hanging() -> None:
    # Should not happen (the worker stores run_finished before finishing the run), but the
    # stream must not hang forever on keep-alives for a run that will never emit again.
    store = _store_with("run-a", _history("run-a")[:2], "failed")

    async def go() -> httpx.Response:
        async with _app(store) as app:
            return await _get(app, "/runs/run-a/events")

    assert [e["id"] for e in _events(_run(go).text)] == [1, 2]


def test_events_terminal_status_without_run_finished_is_logged_as_a_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    store = _store_with("run-a", _history("run-a")[:2], "failed")

    async def go() -> httpx.Response:
        async with _app(store) as app:
            return await _get(app, "/runs/run-a/events")

    with caplog.at_level(logging.DEBUG, logger="sluice.api.routes_events"):
        _run(go)
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "no run_finished" in warnings[0].getMessage()


def test_events_resume_past_the_end_of_a_finished_run_is_not_a_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    store = _store_with("run-a", _history("run-a"), "succeeded")

    async def go() -> httpx.Response:
        async with _app(store) as app:
            return await _get(app, "/runs/run-a/events", {"Last-Event-ID": "3"})

    with caplog.at_level(logging.DEBUG, logger="sluice.api.routes_events"):
        body = _run(go).text
    assert _events(body) == []
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_keepalive_default_is_well_inside_the_contract_bound() -> None:
    assert ApiSettings().keepalive_s == 10.0  # the contract: at least every 15 s


def test_events_activity_is_stored_and_replayed_after_last_event_id() -> None:
    activity = ev.Activity(
        node_id=None, phase="verifying", message="Re-running the rules on the repaired data"
    )
    events = _stamp("run-a", started("run-a"), activity, finished())
    store = _store_with("run-a", events, "succeeded")

    async def go() -> httpx.Response:
        async with _app(store) as app:
            return await _get(app, "/runs/run-a/events", {"Last-Event-ID": "1"})

    res = _run(go)
    got = _events(res.text)
    assert [e["id"] for e in got] == [2, 3]
    assert got[0]["data"] == events[1].to_dict()
    assert PIPELINE_EVENT_ADAPTER.validate_python(got[0]["data"]) == events[1]
