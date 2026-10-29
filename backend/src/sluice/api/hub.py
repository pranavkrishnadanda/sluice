"""In-process fan-out of live run events to SSE subscribers (``docs/api/contract.md``).

The run store is the source of truth; the hub only carries what happens *now*. The worker
appends every event to the store first and publishes it here second, so a stream that replays
from the store and then follows the hub never misses an event (it drops ``seq`` it already
sent). The SSE route should therefore **subscribe first, then replay**, then skip live events
with ``seq <=`` the last one replayed.

Threading: :meth:`EventHub.publish` and :meth:`EventHub.close` may be called from any thread
(the worker runs pipelines in ``asyncio.to_thread``); both hand off to the hub's event loop with
``loop.call_soon_threadsafe``, so every delivery happens on the loop, in call order. A publish
after the loop has closed (shutdown) is dropped. :meth:`subscribe` and
:meth:`subscriber_count` must be called on the loop.

Back-pressure: each subscription buffers at most :data:`MAX_QUEUED_EVENTS` events. A subscriber
that falls further behind is closed (``overflowed`` is set) after the events it already holds;
it must reconnect and replay from the store. A ``run_finished`` closes every subscription of its
run once delivered. Memory: O(subscribers x buffered events), bounded by the cap.
"""

import asyncio
import logging
from collections import deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Final, Self

from sluice.agent.events import EventModel, RunFinished

__all__ = ["MAX_QUEUED_EVENTS", "EventHub", "Subscription"]

_log = logging.getLogger(__name__)

MAX_QUEUED_EVENTS: Final = 1_000
"""Events one subscription may buffer before it is closed as too slow."""


class Subscription:
    """One subscriber's bounded buffer of a run's live events; an async iterator.

    Iteration yields buffered events in order and stops once the subscription is closed and
    drained (run finished, :meth:`EventHub.close`, or overflow).
    """

    __slots__ = ("_buffer", "_closed", "_max", "_wake", "overflowed")

    def __init__(self, max_events: int = MAX_QUEUED_EVENTS) -> None:
        self._buffer: deque[EventModel] = deque()
        self._closed = False
        self._max = max_events
        self._wake = asyncio.Event()
        self.overflowed = False
        """``True`` when the subscriber fell behind and was closed: reconnect and replay."""

    def _push(self, event: EventModel) -> bool:
        """Buffer ``event``; ``False`` once closed (overflow closes it)."""
        if self._closed:
            return False
        if len(self._buffer) >= self._max:
            self.overflowed = True
            self._close()
            return False
        self._buffer.append(event)
        self._wake.set()
        return True

    def _close(self) -> None:
        self._closed = True
        self._wake.set()

    def __aiter__(self) -> Self:
        return self

    async def __anext__(self) -> EventModel:
        while True:
            if self._buffer:
                return self._buffer.popleft()
            if self._closed:
                raise StopAsyncIteration
            self._wake.clear()
            await self._wake.wait()


class EventHub:
    """Live events by run id. Create it on the event loop that serves the subscribers.

    Raises:
        RuntimeError: Constructed outside a running event loop.
    """

    __slots__ = ("_loop", "_subs")

    def __init__(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._subs: dict[str, set[Subscription]] = {}

    def publish(self, run_id: str, event: EventModel) -> None:
        """Deliver ``event`` to the run's subscribers (thread-safe, non-blocking)."""
        self._schedule(self._deliver, run_id, event)

    def close(self, run_id: str) -> None:
        """End every subscription of the run after the events already published (thread-safe)."""
        self._schedule(self._close_run, run_id)

    def subscriber_count(self, run_id: str) -> int:
        """Open subscriptions of the run (call on the loop)."""
        return len(self._subs.get(run_id, ()))

    @asynccontextmanager
    async def subscribe(self, run_id: str) -> AsyncIterator[Subscription]:
        """Follow the run's live events from now on; unsubscribes on exit."""
        sub = Subscription()
        self._subs.setdefault(run_id, set()).add(sub)
        try:
            yield sub
        finally:
            self._discard(run_id, sub)

    # ----------------------------------------------------------------------------------------

    def _schedule(self, callback: object, *args: object) -> None:
        try:
            self._loop.call_soon_threadsafe(callback, *args)  # type: ignore[arg-type]
        except RuntimeError:
            # The loop is closed (shutdown); the store still holds the event for replays.
            _log.debug("event hub loop closed; live event dropped")

    def _deliver(self, run_id: str, event: EventModel) -> None:
        subs = self._subs.get(run_id)
        if not subs:
            return
        for sub in list(subs):
            if not sub._push(event):
                self._discard(run_id, sub)
        if isinstance(event, RunFinished):
            self._close_run(run_id)

    def _close_run(self, run_id: str) -> None:
        for sub in self._subs.pop(run_id, set()):
            sub._close()

    def _discard(self, run_id: str, sub: Subscription) -> None:
        subs = self._subs.get(run_id)
        if subs is None:
            return
        subs.discard(sub)
        if not subs:
            del self._subs[run_id]
