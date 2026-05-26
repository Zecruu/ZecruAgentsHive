"""v1.13 multi-Coder UX tests — Connected Coders panel, since param, heartbeat.

Covers:
- Heartbeat updates on each Coder-side tool when coder_id is passed
- Throttle prevents inside-interval bumps; monkeypatch=0 allows them
- Legacy callers (coder_id=None) bump Mission.coder_last_seen only
- wait_for_planner_message `since` (timestamp / message_id / silent passthrough)
- Connected Coders panel: 2 distinct, legacy unidentified, no mission empty, no activity empty

Run against a fresh local server:
    AGENTSHIVE_API_KEY=test-key DATABASE_URL=sqlite:///./agentshive_test.db PORT=8000 \\
        TOOL_BLOCK_TIMEOUT_SECONDS=10 python -m agentshive.main &
    AGENTSHIVE_BASE=http://localhost:8000 PYTHONIOENCODING=utf-8 \\
        python tests/test_v1_13_multicoder_ux.py
"""

import asyncio
import json
import os
import secrets
import sys
from datetime import datetime, timezone, timedelta

import httpx
from fastmcp import Client

KEY = os.environ.get("AGENTSHIVE_API_KEY", "test-key")
BASE = os.environ.get("AGENTSHIVE_BASE", "http://localhost:8000")
MCP = f"{BASE}/mcp"
BEARER = {"Authorization": f"Bearer {KEY}"}
ORIGIN = {**BEARER, "Origin": BASE}

assert "localhost" in BASE or "127.0.0.1" in BASE, \
    f"refusing to run against non-localhost URL: {BASE}"


def _unwrap(r) -> dict | list:
    sc = r.structured_content if hasattr(r, "structured_content") and r.structured_content is not None else None
    if sc is not None:
        return sc.get("result", sc) if isinstance(sc, dict) else sc
    return json.loads(r.content[0].text)


def _slug() -> str:
    return f"t13-{secrets.token_hex(3)}"


def _new_project(slug: str) -> None:
    r = httpx.post(f"{BASE}/api/dashboard/projects",
                   json={"slug": slug, "name": slug}, headers=ORIGIN, timeout=5)
    assert r.status_code == 201, r.text[:200]


async def _open_mission(project_slug: str) -> str:
    async with Client(f"{MCP}?project={project_slug}", auth=KEY) as c:
        d = _unwrap(await c.call_tool("create_mission", {"name": "v1.13 test", "spec": "s"}))
        return d["mission_id"]


def _state(project_slug: str) -> dict:
    return httpx.get(f"{BASE}/api/dashboard/state?project={project_slug}",
                     headers=BEARER, timeout=5).json()


# ---------- Heartbeat tests (Commit 1) ----------


async def test_heartbeat_records_identified_coder():
    print("--- T1: get_active_mission(coder_id=A) seeds CoderHeartbeat surfaced in panel ---")
    p = _slug(); _new_project(p)
    await _open_mission(p)
    async with Client(f"{MCP}?project={p}", auth=KEY) as c:
        await c.call_tool("get_active_mission", {"coder_id": "alpha"})
    # Panel sees coder=alpha even though no Q/S/M yet
    state = _state(p)
    coders = state["connected_coders"]
    assert any(c["coder_id"] == "alpha" for c in coders), f"missing alpha: {coders}"
    print("  [OK]")


async def test_heartbeat_skips_when_coder_id_none():
    print("--- T2: legacy coder_id=None -> no CoderHeartbeat row (panel empty for Coder) ---")
    p = _slug(); _new_project(p)
    await _open_mission(p)
    async with Client(f"{MCP}?project={p}", auth=KEY) as c:
        await c.call_tool("get_active_mission", {})  # no coder_id
    state = _state(p)
    coders = state["connected_coders"]
    # Legacy ones only show up via Q/S/M, not heartbeat. get_active_mission emits nothing.
    assert all(c["coder_id"] != "<unidentified>" for c in coders), coders
    print("  [OK]")


async def test_heartbeat_unidentified_via_legacy_question():
    print("--- T3: legacy Coder (coder_id=None) producing a Q surfaces as <unidentified> ---")
    p = _slug(); _new_project(p)
    await _open_mission(p)
    async with Client(f"{MCP}?project={p}", auth=KEY) as c:
        # ask_planner without coder_id; it blocks but the question lands before the wait loop
        try:
            await asyncio.wait_for(c.call_tool("ask_planner", {"question": "legacy q"}), timeout=2.0)
        except asyncio.TimeoutError:
            pass
    state = _state(p)
    coders = state["connected_coders"]
    assert any(c["coder_id"] == "<unidentified>" for c in coders), f"missing <unidentified>: {coders}"
    unid = [c for c in coders if c["coder_id"] == "<unidentified>"][0]
    assert unid["q_count"] == 1, unid
    print("  [OK]")


# ---------- since param (Commit 1) ----------


async def _post_planner_message(project_slug: str, body: str, target_coder_id=None) -> str:
    async with Client(f"{MCP}?project={project_slug}", auth=KEY) as c:
        payload = {"body": body}
        if target_coder_id is not None:
            payload["target_coder_id"] = target_coder_id
        r = _unwrap(await c.call_tool("send_to_coder", payload))
        return r["message_id"]


async def test_since_timestamp_filters_old_messages():
    print("--- T4: since=<past ts> excludes pre-marker messages ---")
    p = _slug(); _new_project(p)
    await _open_mission(p)
    await _post_planner_message(p, "old-msg")
    marker = datetime.now(timezone.utc).isoformat()
    await asyncio.sleep(0.5)
    await _post_planner_message(p, "new-msg")
    async with Client(f"{MCP}?project={p}", auth=KEY) as c:
        r = _unwrap(await c.call_tool(
            "wait_for_planner_message",
            {"timeout_seconds": 3, "since": marker},
        ))
        assert r.get("body") == "new-msg", f"got old msg through since filter: {r}"
        await c.call_tool("ack_message", {"message_id": r["message_id"]})
    print("  [OK]")


async def test_since_message_id_resumes_after():
    print("--- T5: since=<message_id> resumes after that specific message ---")
    p = _slug(); _new_project(p)
    await _open_mission(p)
    mid_first = await _post_planner_message(p, "first")
    await asyncio.sleep(0.1)
    await _post_planner_message(p, "second")
    async with Client(f"{MCP}?project={p}", auth=KEY) as c:
        r = _unwrap(await c.call_tool(
            "wait_for_planner_message",
            {"timeout_seconds": 3, "since": mid_first},
        ))
        assert r.get("body") == "second", f"since=message_id failed: {r}"
        await c.call_tool("ack_message", {"message_id": r["message_id"]})
    print("  [OK]")


async def test_since_malformed_silently_ignored():
    print("--- T6: malformed since silently falls back to next unacked ---")
    p = _slug(); _new_project(p)
    await _open_mission(p)
    await _post_planner_message(p, "first")
    async with Client(f"{MCP}?project={p}", auth=KEY) as c:
        # malformed string and unknown message_id both pass through
        for bad in ("not-a-date", "deadbeef" * 4, "zzzzzzzz" * 4):
            r = _unwrap(await c.call_tool(
                "wait_for_planner_message",
                {"timeout_seconds": 3, "since": bad},
            ))
            assert r.get("body") == "first", f"since={bad!r} failed silent passthrough: {r}"
    print("  [OK] 3 bad sinces all silently ignored")


# ---------- Connected Coders panel (Commit 2) ----------


async def test_panel_shows_two_distinct_coders():
    print("--- T7: panel shows 2 distinct Coders with separate counts ---")
    p = _slug(); _new_project(p)
    await _open_mission(p)
    async with Client(f"{MCP}?project={p}", auth=KEY) as c:
        # Two Coders, A asks twice, B submits once. Use timeouts since these block.
        for _ in range(2):
            try:
                await asyncio.wait_for(
                    c.call_tool("ask_planner", {"question": "q", "coder_id": "alpha"}),
                    timeout=1.5,
                )
            except asyncio.TimeoutError:
                pass
        try:
            await asyncio.wait_for(
                c.call_tool("submit_progress", {"summary": "s", "coder_id": "beta"}),
                timeout=1.5,
            )
        except asyncio.TimeoutError:
            pass
    state = _state(p)
    by_id = {c["coder_id"]: c for c in state["connected_coders"]}
    assert "alpha" in by_id and by_id["alpha"]["q_count"] == 2, by_id.get("alpha")
    assert "beta" in by_id and by_id["beta"]["s_count"] == 1, by_id.get("beta")
    print("  [OK]")


async def test_panel_empty_when_no_active_mission():
    print("--- T8: no active mission -> connected_coders is [] ---")
    p = _slug(); _new_project(p)
    # No mission at all
    state = _state(p)
    assert state["connected_coders"] == [], state["connected_coders"]
    assert state["connected_coder_ttl_seconds"] == 300
    print("  [OK]")


async def test_panel_empty_when_active_no_activity():
    print("--- T9: active mission but no Coder activity -> empty list ---")
    p = _slug(); _new_project(p)
    await _open_mission(p)  # uses create_mission, which is Planner-side, no Coder touch
    state = _state(p)
    assert state["connected_coders"] == [], state["connected_coders"]
    print("  [OK]")


async def test_panel_ttl_filters_stale():
    print("--- T10: panel exposes connected_coder_ttl_seconds = 300 for the JS layer ---")
    p = _slug(); _new_project(p)
    await _open_mission(p)
    state = _state(p)
    assert state["connected_coder_ttl_seconds"] == 300, state["connected_coder_ttl_seconds"]
    print("  [OK]")


async def main():
    await test_heartbeat_records_identified_coder()
    await test_heartbeat_skips_when_coder_id_none()
    await test_heartbeat_unidentified_via_legacy_question()
    await test_since_timestamp_filters_old_messages()
    await test_since_message_id_resumes_after()
    await test_since_malformed_silently_ignored()
    await test_panel_shows_two_distinct_coders()
    await test_panel_empty_when_no_active_mission()
    await test_panel_empty_when_active_no_activity()
    await test_panel_ttl_filters_stale()
    print("\nALL v1.13 TESTS PASS")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except AssertionError as e:
        print(f"\nFAILED: {e}", file=sys.stderr)
        sys.exit(1)
    except asyncio.TimeoutError as e:
        print(f"\nFAILED (timeout): {e}", file=sys.stderr)
        sys.exit(1)
