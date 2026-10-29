"""``GET /runs/{id}/events``: a run's events as ``text/event-stream`` (``docs/api/contract.md``).

Each message is ``id: <seq>\\ndata: <RunEvent JSON, camelCase, one line>\\n\\n``; a comment
``: keep-alive\\n\\n`` is sent whenever nothing else was sent for ``ApiSettings.keepalive_s``.

Algorithm (per connection), so no event is lost or sent twice:

1. **Subscribe** to the hub first, so nothing published from now on is missed.
2. **Replay** ``store.events_after(id, last_sent)``. The worker stores an event before it
   publishes it, so the replay holds everything published before the subscription began.
3. **Follow** the subscription, skipping ``seq <= last_sent`` (the subscribe/replay overlap).
4. Close after sending ``run_finished``.

If the subscription ends before ``run_finished`` was sent (the hub closed the run, or this
subscriber overflowed), the stream goes back to step 1 from ``last_sent``: the store has every
event the hub dropped, so the client still gets each event once and ends on ``run_finished``.
The only way the stream ends without ``run_finished`` is a run whose stored status is terminal but
whose ``run_finished`` is not stored (the worker rules this out); the stream then closes rather
than sending keep-alives forever.

Resume: ``Last-Event-ID`` is the last ``seq`` the client applied; a non-negative integer resumes
after it, anything else replays from ``seq 1``. On client disconnect Starlette cancels the
generator, and the ``subscribe`` context manager releases the subscription.

Cost: one ``get_run`` and one ``events_after`` per (re)subscription, both run in a thread (they
block on Postgres). ``events_after`` is unbounded: fine for v1, whose runs emit tens of events.
Memory: the replay page plus the hub's bounded per-subscriber buffer.
"""

import asyncio
import logging
import re
from collections.abc import AsyncIterator
from typing import Annotated, Final

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import StreamingResponse

from sluice.agent.events import EventModel, RunFinished
from sluice.api.hub import EventHub
from sluice.api.store import TERMINAL_STATUSES, RunStore

__all__ = ["KEEPALIVE", "SSE_HEADERS", "format_event", "resume_seq", "router"]

_log = logging.getLogger(__name__)

KEEPALIVE: Final = ": keep-alive\n\n"
"""SSE comment that keeps proxies and the client from seeing an idle connection."""
SSE_HEADERS: Final = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
"""No caching, and no response buffering by nginx-style proxies."""
MAX_SEQ: Final = 2**63 - 1
"""Largest resume point (Postgres ``bigint``); larger ``Last-Event-ID`` values are ignored."""
_DIGITS: Final = re.compile(r"[0-9]{1,19}")

router = APIRouter()


def resume_seq(last_event_id: str | None) -> int:
    """``int(Last-Event-ID)`` when it is a non-negative (ASCII) integer that fits a
    ``bigint``, else ``0`` (replay from the start)."""
    if last_event_id is None or not _DIGITS.fullmatch(last_event_id):
        return 0
    seq = int(last_event_id)
    return seq if seq <= MAX_SEQ else 0


def format_event(event: EventModel) -> str:
    """One SSE message: ``id: <seq>``, one ``data:`` line of camelCase JSON, a blank line."""
    # Compact JSON escapes control characters, so the payload is always a single line.
    return f"id: {event.seq}\ndata: {event.to_json()}\n\n"


async def _stream(
    run_id: str, resume: int, store: RunStore, hub: EventHub, keepalive_s: float
) -> AsyncIterator[str]:
    """The run's events after ``resume``, live until ``run_finished`` (see module docstring)."""
    last_sent = resume
    while True:
        async with hub.subscribe(run_id) as sub:
            # Status before replay: a terminal status implies its run_finished is already stored.
            row = await asyncio.to_thread(store.get_run, run_id)
            for event in await asyncio.to_thread(store.events_after, run_id, last_sent):
                yield format_event(event)
                last_sent = event.seq
                if isinstance(event, RunFinished):
                    return
            if row is None or row.status in TERMINAL_STATUSES:
                # Already sent (resume past the end) or, abnormally, never stored: either way
                # nothing more will come, so close instead of sending keep-alives forever.
                _log.debug("run %s is finished; no run_finished after seq %d", run_id, last_sent)
                return
            live = aiter(sub)
            while True:
                try:
                    event = await asyncio.wait_for(anext(live), keepalive_s)
                except TimeoutError:
                    yield KEEPALIVE
                    continue
                except StopAsyncIteration:
                    break
                if event.seq <= last_sent:
                    continue
                yield format_event(event)
                last_sent = event.seq
                if isinstance(event, RunFinished):
                    return
        _log.debug(
            "run %s: subscription ended (overflowed=%s); replaying from seq %d",
            run_id,
            sub.overflowed,
            last_sent,
        )


@router.get("/runs/{run_id}/events")
async def run_events(
    run_id: str,
    request: Request,
    last_event_id: Annotated[str | None, Header()] = None,
) -> StreamingResponse:
    """Stream the run's events (replay, then live); ``404`` for an unknown run."""
    state = request.app.state
    store: RunStore = state.store
    if await asyncio.to_thread(store.get_run, run_id) is None:
        raise HTTPException(status_code=404, detail="Run not found.")
    return StreamingResponse(
        _stream(run_id, resume_seq(last_event_id), store, state.hub, state.settings.keepalive_s),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )
