"""v1.6 SSE push channel tests.

Covers /api/dashboard/events:
  T1 no auth -> 401
  T2 bearer -> 200 + Content-Type: text/event-stream
  T3 initial event arrives within ~1s, carries the state envelope
  T4 push event arrives within ~3s after another caller mutates state
  T5 (slow, gated by SLOW_TESTS=1) keepalive comment arrives within ~17s
  T6 multiple subscribers each receive the same push event

Run against a fresh local server:
    AGENTSHIVE_API_KEY=test-key PORT=8001 TOOL_BLOCK_TIMEOUT_SECONDS=20 \
        python -m agentshive.main &
    python tests/test_dashboard_sse.py
    SLOW_TESTS=1 python tests/test_dashboard_sse.py   # includes T5
"""

import asyncio
import json
import os
import sys

import httpx
from fastmcp import Client

KEY = os.environ.get("AGENTSHIVE_API_KEY", "test-key")
BASE = os.environ.get("AGENTSHIVE_BASE", "http://localhost:8000")
MCP_URL = f"{BASE}/mcp"
BEARER = {"Authorization": f"Bearer {KEY}"}


async def _event_iter(resp, kind: str = "state"):
    """Persistent async generator over the SSE stream -- yields parsed `event: <kind>`
    payloads. Keep a single iterator across multiple reads in a test (httpx's
    aiter_bytes() raises StreamConsumed if iterated more than once on the same response).
    """
    buf = b""
    async for chunk in resp.aiter_bytes():
        buf += chunk
        while b"\n\n" in buf:
            block, buf = buf.split(b"\n\n", 1)
            text = block.decode("utf-8", errors="replace")
            if f"event: {kind}" in text:
                for line in text.splitlines():
                    if line.startswith("data: "):
                        yield json.loads(line[6:])
                        break


async def _next_event(ait, timeout: float = 5.0) -> dict:
    """Wait for one event from a persistent iterator with a per-call timeout."""
    return await asyncio.wait_for(ait.__anext__(), timeout=timeout)


def test_no_auth_returns_401():
    print("--- T1: GET /api/dashboard/events no auth -> 401 ---")
    r = httpx.get(f"{BASE}/api/dashboard/events", timeout=5)
    assert r.status_code == 401, f"expected 401, got {r.status_code}"
    print("  [OK]")


async def test_bearer_returns_event_stream():
    print("--- T2: bearer -> 200 + Content-Type: text/event-stream ---")
    async with httpx.AsyncClient(timeout=5) as cli:
        async with cli.stream("GET", f"{BASE}/api/dashboard/events", headers=BEARER) as resp:
            assert resp.status_code == 200, f"got {resp.status_code}"
            ct = resp.headers.get("content-type", "")
            assert "text/event-stream" in ct, f"wrong content-type: {ct}"
            ait = _event_iter(resp)
            evt = await _next_event(ait, timeout=3.0)
            assert isinstance(evt, dict)
            print(f"  [OK] 200 + content-type={ct}")


async def test_initial_event_carries_state_envelope():
    print("--- T3: initial event arrives within ~1s with the state envelope ---")
    async with httpx.AsyncClient(timeout=5) as cli:
        async with cli.stream("GET", f"{BASE}/api/dashboard/events", headers=BEARER) as resp:
            ait = _event_iter(resp)
            evt = await _next_event(ait, timeout=2.0)
            required = {"active_mission", "pending_questions", "pending_summaries",
                        "messages", "server_info", "coder_heartbeat"}
            missing = required - set(evt.keys())
            assert not missing, f"missing keys: {missing}"
            print(f"  [OK] state envelope shape correct ({len(evt)} top-level keys)")


async def test_push_on_write():
    print("--- T4: push event arrives after a write from another caller ---")
    async with httpx.AsyncClient(timeout=10) as cli:
        async with cli.stream("GET", f"{BASE}/api/dashboard/events", headers=BEARER) as resp:
            ait = _event_iter(resp)
            await _next_event(ait, timeout=2.0)   # drain initial
            async def writer():
                await asyncio.sleep(0.3)
                async with Client(MCP_URL, auth=KEY) as mcli:
                    await mcli.call_tool("create_mission", {"name": "sse-push-test", "spec": "trigger broadcast"})
            asyncio.create_task(writer())
            evt = await _next_event(ait, timeout=5.0)
            assert evt.get("active_mission") and evt["active_mission"]["name"] == "sse-push-test", \
                f"push event missing or wrong: {json.dumps(evt)[:200]}"
            print(f"  [OK] push delivered with new active mission name")


async def test_keepalive_in_idle_window():
    if not os.environ.get("SLOW_TESTS"):
        print("--- T5: keepalive comment in idle window -- SKIPPED (set SLOW_TESTS=1 to run) ---")
        return
    print("--- T5: keepalive (`: keepalive`) comment arrives within ~17s of idle ---")
    async with httpx.AsyncClient(timeout=25) as cli:
        async with cli.stream("GET", f"{BASE}/api/dashboard/events", headers=BEARER) as resp:
            # Drain the initial event
            await _read_one_event(resp, "state", timeout=2.0)
            # Now wait for any traffic -- looking for a `: keepalive` line specifically.
            buf = b""
            deadline = asyncio.get_event_loop().time() + 18
            seen_keepalive = False
            async for chunk in resp.aiter_bytes():
                if asyncio.get_event_loop().time() > deadline:
                    break
                buf += chunk
                if b": keepalive" in buf:
                    seen_keepalive = True
                    break
            assert seen_keepalive, f"no keepalive seen in 17s; buffered: {buf[:200]!r}"
            print(f"  [OK] keepalive emitted")


async def test_multiple_subscribers_both_receive():
    print("--- T6: multiple subscribers each receive the same push event ---")
    async with httpx.AsyncClient(timeout=10) as c1, httpx.AsyncClient(timeout=10) as c2:
        async with c1.stream("GET", f"{BASE}/api/dashboard/events", headers=BEARER) as r1, \
                   c2.stream("GET", f"{BASE}/api/dashboard/events", headers=BEARER) as r2:
            ait1 = _event_iter(r1)
            ait2 = _event_iter(r2)
            await _next_event(ait1, timeout=2.0)
            await _next_event(ait2, timeout=2.0)
            async def writer():
                await asyncio.sleep(0.3)
                async with Client(MCP_URL, auth=KEY) as mcli:
                    await mcli.call_tool("create_mission", {"name": "sse-multisub", "spec": "broadcast me"})
            asyncio.create_task(writer())
            e1, e2 = await asyncio.gather(
                _next_event(ait1, timeout=5.0),
                _next_event(ait2, timeout=5.0),
            )
            assert e1["active_mission"]["name"] == "sse-multisub"
            assert e2["active_mission"]["name"] == "sse-multisub"
            print(f"  [OK] both subscribers received the push event")


async def main():
    test_no_auth_returns_401()
    await test_bearer_returns_event_stream()
    await test_initial_event_carries_state_envelope()
    await test_push_on_write()
    await test_keepalive_in_idle_window()
    await test_multiple_subscribers_both_receive()
    print("\nALL SSE TESTS PASS")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except AssertionError as e:
        print(f"\nFAILED: {e}", file=sys.stderr)
        sys.exit(1)
    except asyncio.TimeoutError as e:
        print(f"\nFAILED (timeout): {e}", file=sys.stderr)
        sys.exit(1)
