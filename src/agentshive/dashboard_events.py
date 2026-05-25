"""In-process pub/sub for the dashboard SSE push channel (v1.6).

Sync writers (`_do_<name>` in tools.py, the nested @mcp.tool bodies) call
`broadcast()` after committing state changes. Async SSE subscribers receive a
sentinel on their personal asyncio.Queue, then rebuild and push the current
state envelope to the connected browser.

Architecture notes:
  - Sync→async hop: writers run on a thread pool or the request thread,
    subscribers run on the uvicorn event loop. `asyncio.run_coroutine_threadsafe`
    schedules a Queue.put onto the captured loop.
  - The loop is captured once at app startup via the Starlette `on_startup`
    hook (see main.py). If `broadcast()` is called before startup ran (test
    setup, race), it silently no-ops — polling catches the change.
  - Per-subscriber queue is bounded (maxsize=100) with drop-oldest on overflow,
    so a slow / dead client can't grow memory without bound. The client resyncs
    on the next event they read.
  - Subscribers register with `subscribe()` (returns id + queue), and MUST call
    `unsubscribe(id)` on disconnect (via try/finally in the SSE handler).
"""

import asyncio
import logging
import uuid
from typing import Optional

log = logging.getLogger(__name__)

_subscribers: dict[str, asyncio.Queue] = {}
_event_loop: Optional[asyncio.AbstractEventLoop] = None

# Any non-None sentinel works — the SSE handler doesn't read it, just uses
# "an item arrived" as the trigger to rebuild + push state.
_SENTINEL = object()
_QUEUE_MAXSIZE = 100


def register_loop(loop: asyncio.AbstractEventLoop) -> None:
    """Called from a Starlette `on_startup` hook so broadcast() knows where to schedule."""
    global _event_loop
    _event_loop = loop


def subscribe() -> tuple[str, asyncio.Queue]:
    """Create a new subscriber. Returns (subscriber_id, queue)."""
    sub_id = uuid.uuid4().hex
    q: asyncio.Queue = asyncio.Queue(maxsize=_QUEUE_MAXSIZE)
    _subscribers[sub_id] = q
    return sub_id, q


def unsubscribe(sub_id: str) -> None:
    """Remove a subscriber. Idempotent — safe to call multiple times."""
    _subscribers.pop(sub_id, None)


def broadcast() -> None:
    """Wake every connected SSE subscriber. Safe to call from sync context.

    No-op if the event loop hasn't been captured yet (early startup, test setup).
    """
    loop = _event_loop
    if loop is None or loop.is_closed() or not _subscribers:
        return

    # Snapshot subscribers — broadcast happens from a different thread than the
    # one that mutates _subscribers via subscribe/unsubscribe (the request loop).
    # Dict ops in CPython are atomic enough for items()/keys() iteration here.
    for sub_id, q in list(_subscribers.items()):
        try:
            asyncio.run_coroutine_threadsafe(_enqueue(q, sub_id), loop)
        except RuntimeError:
            # Loop closed mid-broadcast — accept the dropped event, polling catches up.
            log.debug("broadcast: loop closed for subscriber %s", sub_id[:8])


async def _enqueue(q: asyncio.Queue, sub_id: str) -> None:
    """Put a sentinel on the subscriber's queue; drop the oldest if full."""
    try:
        q.put_nowait(_SENTINEL)
    except asyncio.QueueFull:
        # Slow / dead client. Drop oldest, push newest. Client resyncs on read.
        try:
            q.get_nowait()
        except asyncio.QueueEmpty:
            pass
        try:
            q.put_nowait(_SENTINEL)
        except asyncio.QueueFull:
            log.warning("broadcast: queue still full after drop for subscriber %s", sub_id[:8])


def subscriber_count() -> int:
    """Diagnostic — how many SSE connections currently subscribed."""
    return len(_subscribers)
