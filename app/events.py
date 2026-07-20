"""In-process pub/sub backing the live-update Server-Sent Events stream.

The app runs as a single uvicorn worker, so a plain in-memory fan-out is enough:
every connected SSE client holds an asyncio.Queue in `_subscribers`, and
`broadcast()` pushes an event onto each one.

If this is ever run with more than one worker, this needs to move to a shared bus
(e.g. Redis pub/sub) — an in-process set only reaches clients attached to the
same worker process.
"""
import asyncio
import json
import logging
from typing import Any

log = logging.getLogger("filmclub.events")

_subscribers: set[asyncio.Queue] = set()


def subscribe() -> asyncio.Queue:
    """Register a new client and return its queue."""
    q: asyncio.Queue = asyncio.Queue(maxsize=100)
    _subscribers.add(q)
    return q


def unsubscribe(q: asyncio.Queue) -> None:
    _subscribers.discard(q)


def broadcast(event: dict[str, Any]) -> None:
    """Fan an event out to every connected client. Never raises."""
    if not _subscribers:
        return
    data = json.dumps(event)
    for q in list(_subscribers):
        try:
            q.put_nowait(data)
        except asyncio.QueueFull:
            # A client that can't keep up: drop this event for it rather than
            # blocking everyone. It resyncs on its next reconnect/navigation.
            log.warning("SSE subscriber queue full; dropping event")
