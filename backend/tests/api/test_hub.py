"""EventHub: thread-safe fan-out of one run's live events to its SSE subscribers.

No services: every test drives the hub inside ``asyncio.run``.
"""

import asyncio
import threading
from collections.abc import Awaitable, Callable
from typing import Any

import pytest

from sluice.agent import events as ev
from sluice.api.hub import MAX_QUEUED_EVENTS, EventHub

AT = "2026-10-27T10:14:03.000Z"


def _event(seq: int) -> ev.EventModel:
    return ev.RulesApplied(actions=[f"a{seq}"], seq=seq, at=AT)


def _finished(seq: int) -> ev.EventModel:
    return ev.RunFinished(status="succeeded", summary="ok", cost_usd=0.0, tokens=0, seq=seq, at=AT)


def _run(make: Callable[[], Awaitable[Any]]) -> Any:
    return asyncio.run(asyncio.wait_for(make(), timeout=5))


async def _settle() -> None:
    """Let every callback scheduled with ``call_soon_threadsafe`` run."""
    for _ in range(5):
        await asyncio.sleep(0)


def test_hub_subscriber_receives_published_events_in_order() -> None:
    async def go() -> list[int]:
        hub = EventHub()
        async with hub.subscribe("r1") as events:
            for seq in (1, 2, 3):
                hub.publish("r1", _event(seq))
            hub.publish("r1", _finished(4))
            return [e.seq async for e in events]

    assert _run(go) == [1, 2, 3, 4]


def test_hub_publish_from_another_thread_reaches_the_subscriber() -> None:
    async def go() -> list[int]:
        hub = EventHub()
        async with hub.subscribe("r1") as events:

            def produce() -> None:
                for seq in (1, 2):
                    hub.publish("r1", _event(seq))
                hub.publish("r1", _finished(3))

            thread = threading.Thread(target=produce)
            thread.start()
            received = [e.seq async for e in events]
            thread.join(2)
            return received

    assert _run(go) == [1, 2, 3]


def test_hub_events_of_other_runs_are_not_delivered() -> None:
    async def go() -> list[int]:
        hub = EventHub()
        async with hub.subscribe("r1") as events:
            hub.publish("r2", _event(1))
            hub.publish("r1", _finished(1))
            return [e.seq async for e in events]

    assert _run(go) == [1]


def test_hub_run_finished_ends_every_subscription_of_the_run() -> None:
    async def go() -> tuple[list[int], list[int]]:
        hub = EventHub()
        async with hub.subscribe("r1") as first, hub.subscribe("r1") as second:
            hub.publish("r1", _event(1))
            hub.publish("r1", _finished(2))
            return [e.seq async for e in first], [e.seq async for e in second]

    assert _run(go) == ([1, 2], [1, 2])


def test_hub_close_ends_subscriptions_after_already_published_events() -> None:
    async def go() -> list[int]:
        hub = EventHub()
        async with hub.subscribe("r1") as events:
            hub.publish("r1", _event(1))
            hub.close("r1")
            received = [e.seq async for e in events]
            return received

    assert _run(go) == [1]


def test_hub_subscriber_count_tracks_open_subscriptions() -> None:
    async def go() -> list[int]:
        hub = EventHub()
        counts = [hub.subscriber_count("r1")]
        async with hub.subscribe("r1"):
            counts.append(hub.subscriber_count("r1"))
            async with hub.subscribe("r1"):
                counts.append(hub.subscriber_count("r1"))
            counts.append(hub.subscriber_count("r1"))
        counts.append(hub.subscriber_count("r1"))
        return counts

    assert _run(go) == [0, 1, 2, 1, 0]


def test_hub_overflowing_subscriber_is_closed_and_marked() -> None:
    async def go() -> tuple[int, bool, int]:
        hub = EventHub()
        async with hub.subscribe("r1") as events:
            for seq in range(1, MAX_QUEUED_EVENTS + 2):
                hub.publish("r1", _event(seq))
            await _settle()
            count_after = hub.subscriber_count("r1")
            received = [e.seq async for e in events]
            return len(received), events.overflowed, count_after

    received, overflowed, count_after = _run(go)
    assert overflowed is True
    assert received <= MAX_QUEUED_EVENTS
    assert count_after == 0


def test_hub_normal_end_is_not_marked_overflowed() -> None:
    async def go() -> bool:
        hub = EventHub()
        async with hub.subscribe("r1") as events:
            hub.publish("r1", _finished(1))
            _ = [e async for e in events]
            return events.overflowed

    assert _run(go) is False


def test_hub_publish_without_subscribers_is_a_no_op() -> None:
    async def go() -> int:
        hub = EventHub()
        hub.publish("r1", _event(1))
        await _settle()
        return hub.subscriber_count("r1")

    assert _run(go) == 0


def test_hub_publish_after_the_loop_closed_is_ignored() -> None:
    holder: dict[str, EventHub] = {}

    async def make() -> None:
        holder["hub"] = EventHub()

    asyncio.run(make())
    holder["hub"].publish("r1", _event(1))  # must not raise from a worker thread at shutdown


def test_hub_requires_a_running_loop() -> None:
    with pytest.raises(RuntimeError):
        EventHub()
