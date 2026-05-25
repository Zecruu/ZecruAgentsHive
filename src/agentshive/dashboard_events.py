"""In-process pub/sub for the dashboard SSE push channel (v1.6, project-scoped v1.9).

Sync writers (`_do_<name>` in tools.py, the nested @mcp.tool bodies) call
`broadcast(project_slug)` after committing state changes. Async SSE subscribers
that connected with a specific project slug receive a sentinel on their personal
asyncio.Queue, then rebuild and push the current state envelope to the connected
browser.

v1.9 scoping:
  - Subscribers are organized as `dict[project_slug, dict[sub_id, queue]]` so a
    write in project A only wakes project A's subscribers — no wasted fanout to
    every connected dashboard.
  - `subscribe(project_slug)` and `broadcast(project_slug)` both require the
    slug; callers read it from the request URL (?project=) or the request-time
    ContextVar (`agentshive.project.current_project`).
  - A broadcast with project_slug=None falls back to "default" so the worst-case
    a bug in the call site can do is mis-wake the default project's dashboards.

Architecture (unchanged from v1.6):
  - Sync→async hop via `asyncio.run_coroutine_threadsafe(_enqueue(q), loop)`.
  - Loop captured at app startup; pre-startup broadcasts no-op silently.
  - Per-subscriber queue bounded (maxsize=100), drop-oldest on overflow.
  - Subscribers MUST call `unsubscribe(project_slug, sub_id)` on disconnect.
"""

import asyncio
import logging
import uuid
from typing import Optional

log = logging.getLogger(__name__)

# project_slug → { sub_id → queue }. Nested dict so broadcast() can iterate
# just the subscribers for the project being written to.
_subscribers_by_project: dict[str, dict[str, asyncio.Queue]] = {}
_event_loop: Optional[asyncio.AbstractEventLoop] = None

# Any non-None sentinel works — the SSE handler doesn't read it, just uses
# "an item arrived" as the trigger to rebuild + push state.
_SENTINEL = object()
_QUEUE_MAXSIZE = 100

DEFAULT_PROJECT = "default"  # avoid importing from .project to prevent cycles


def register_loop(loop: asyncio.AbstractEventLoop) -> None:
    """Called from the SSE route's lazy loop-capture so broadcast() knows where to schedule."""
    global _event_loop
    _event_loop = loop


def subscribe(project_slug: str) -> tuple[str, asyncio.Queue]:
    """Create a new subscriber scoped to `project_slug`. Returns (subscriber_id, queue).

    Each project gets its own subscriber dict so broadcast(project_slug) only
    walks that project's subscribers. Callers MUST pass `unsubscribe(project_slug, sub_id)`
    on disconnect.
    """
    sub_id = uuid.uuid4().hex
    q: asyncio.Queue = asyncio.Queue(maxsize=_QUEUE_MAXSIZE)
    project_subs = _subscribers_by_project.setdefault(project_slug, {})
    project_subs[sub_id] = q
    return sub_id, q


def unsubscribe(project_slug: str, sub_id: str) -> None:
    """Remove a subscriber. Idempotent — safe to call multiple times.

    Cleans up the project's nested dict entirely once empty so a project with
    no listeners doesn't leak.
    """
    project_subs = _subscribers_by_project.get(project_slug)
    if project_subs is None:
        return
    project_subs.pop(sub_id, None)
    if not project_subs:
        _subscribers_by_project.pop(project_slug, None)


def broadcast(project_slug: Optional[str] = None) -> None:
    """Wake every SSE subscriber connected to `project_slug`. Safe from sync context.

    No-op if the event loop hasn't been captured yet (early startup, test setup).
    A None slug falls back to the default project — protects against call sites
    that lose context.
    """
    loop = _event_loop
    if loop is None or loop.is_closed():
        return
    slug = project_slug or DEFAULT_PROJECT
    project_subs = _subscribers_by_project.get(slug)
    if not project_subs:
        return
    # Snapshot — broadcast can race with subscribe/unsubscribe (different threads).
    for sub_id, q in list(project_subs.items()):
        try:
            asyncio.run_coroutine_threadsafe(_enqueue(q, sub_id), loop)
        except RuntimeError:
            log.debug("broadcast: loop closed for subscriber %s", sub_id[:8])


async def _enqueue(q: asyncio.Queue, sub_id: str) -> None:
    """Put a sentinel on the subscriber's queue; drop the oldest if full."""
    try:
        q.put_nowait(_SENTINEL)
    except asyncio.QueueFull:
        try:
            q.get_nowait()
        except asyncio.QueueEmpty:
            pass
        try:
            q.put_nowait(_SENTINEL)
        except asyncio.QueueFull:
            log.warning("broadcast: queue still full after drop for subscriber %s", sub_id[:8])


def subscriber_count(project_slug: Optional[str] = None) -> int:
    """Diagnostic — total subscribers (no arg) or per-project (with arg)."""
    if project_slug is None:
        return sum(len(subs) for subs in _subscribers_by_project.values())
    return len(_subscribers_by_project.get(project_slug, {}))
