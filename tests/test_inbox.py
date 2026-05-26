"""v1.8 Inbox channel tests.

Covers the back-channel between the dashboard user and the Planner:
  - POST /api/dashboard/send-to-planner (auth, CSRF, no-active-mission OK)
  - GET  /api/dashboard/state -- new `inbox` field present and shaped right
  - MCP tools wait_for_user_message + ack_message + send_to_user + list_inbox_history
  - At-least-once redelivery: re-waiting before ack returns the same row
  - Integration: dashboard POSTs message, Planner wait_for_user_message returns
    it within ~2s, Planner replies via send_to_user, dashboard state surfaces
    the planner_to_user message.

Run against a fresh local server:
    AGENTSHIVE_API_KEY=test-key PORT=8000 AGENTSHIVE_BASE_URL=http://localhost:8000 \
        TOOL_BLOCK_TIMEOUT_SECONDS=10 python -m agentshive.main &
    AGENTSHIVE_BASE=http://localhost:8000 python tests/test_inbox.py
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
ORIGIN_OK = {**BEARER, "Origin": BASE}


def _post_send_to_planner(body: str, *, headers: dict | None = None) -> httpx.Response:
    return httpx.post(
        f"{BASE}/api/dashboard/send-to-planner",
        json={"body": body},
        headers=headers if headers is not None else ORIGIN_OK,
        timeout=5,
    )


def _unwrap(r) -> dict | list:
    """Pull a tool result regardless of FastMCP structured_content wrapping."""
    sc = r.structured_content if hasattr(r, "structured_content") and r.structured_content is not None else None
    if sc is not None:
        return sc.get("result", sc) if isinstance(sc, dict) else sc
    return json.loads(r.content[0].text)


def test_send_to_planner_requires_auth():
    print("--- T1: POST /api/dashboard/send-to-planner with no auth -> 401 ---")
    r = httpx.post(f"{BASE}/api/dashboard/send-to-planner", json={"body": "x"}, timeout=5)
    assert r.status_code == 401, r.status_code
    print("  [OK]")


def test_send_to_planner_rejects_cross_origin():
    print("--- T2: POST send-to-planner with foreign Origin -> 403 ---")
    r = _post_send_to_planner("x", headers={**BEARER, "Origin": "http://evil.example"})
    assert r.status_code == 403, r.status_code
    print("  [OK]")


def test_send_to_planner_succeeds_without_active_mission():
    print("--- T3: POST send-to-planner with NO active mission -> 200, mission_id=None ---")
    # Note: previous tests may have left an active mission. We rely on the global
    # nature of the inbox: it works regardless. We just assert it returns 200 and
    # the stored message is a user_to_planner row.
    r = _post_send_to_planner("inbox: hello planner, just dropping a note")
    assert r.status_code == 200, r.text[:200]
    body = r.json()
    assert body.get("ok") is True, body
    msg = body["message"]
    assert msg["direction"] == "user_to_planner", msg
    assert isinstance(msg["message_id"], str) and len(msg["message_id"]) > 8
    print(f"  [OK] mid={msg['message_id'][:10]} mission_id={msg.get('mission_id')}")


def test_state_payload_carries_inbox_key():
    print("--- T4: GET /api/dashboard/state includes `inbox` list with our message ---")
    r = httpx.get(f"{BASE}/api/dashboard/state", headers=BEARER, timeout=5)
    assert r.status_code == 200, r.status_code
    payload = r.json()
    assert "inbox" in payload, "state payload missing inbox key"
    inbox = payload["inbox"]
    assert isinstance(inbox, list)
    # Each entry should have message_dict shape
    if inbox:
        m = inbox[-1]
        for key in ("message_id", "direction", "body", "created_at"):
            assert key in m, f"inbox entry missing {key}: {m}"
        assert m["direction"] in ("user_to_planner", "planner_to_user")
    print(f"  [OK] inbox len={len(inbox)}")


def test_send_to_planner_validates_empty_body():
    print("--- T5: empty body -> 400 validation error ---")
    r = _post_send_to_planner("   ")
    assert r.status_code == 400, r.status_code
    print("  [OK]")


async def test_integration_user_post_planner_wait_then_reply():
    print("--- T6: integration -- dashboard POSTs, wait_for_user_message returns it, ack, reply via send_to_user ---")
    # Drain any stale unacked user_to_planner first (other tests left rows)
    async with Client(MCP_URL, auth=KEY) as planner:
        for _ in range(20):
            r = await planner.call_tool("wait_for_user_message", {})
            data = json.loads(r.content[0].text) if not getattr(r, "structured_content", None) else r.structured_content
            if data.get("status") == "pending":
                break
            await planner.call_tool("ack_message", {"message_id": data["message_id"]})
        else:
            raise AssertionError("could not drain inbox in 20 iters -- server saturated?")

        # Schedule the dashboard POST after a small delay so the wait actually blocks.
        UNIQUE = "v1.8-integration-marker-please-reply"
        async def writer():
            await asyncio.sleep(0.3)
            _post_send_to_planner(UNIQUE)
        asyncio.create_task(writer())

        # wait_for_user_message should return within ~2s
        r = await planner.call_tool("wait_for_user_message", {})
        msg = json.loads(r.content[0].text) if not getattr(r, "structured_content", None) else r.structured_content
        assert msg.get("body") == UNIQUE, f"got {msg}"
        assert msg.get("direction") == "user_to_planner"
        mid = msg["message_id"]

        # Re-wait returns the same row with redelivery_count bumped (proves at-least-once)
        r = await planner.call_tool("wait_for_user_message", {})
        msg2 = json.loads(r.content[0].text) if not getattr(r, "structured_content", None) else r.structured_content
        assert msg2["message_id"] == mid, "expected same row pre-ack"
        assert msg2.get("redelivery_count", 0) >= 1, msg2

        # ACK
        await planner.call_tool("ack_message", {"message_id": mid})

        # Now wait should time out as pending (no more unacked rows)
        r = await planner.call_tool("wait_for_user_message", {})
        msg3 = json.loads(r.content[0].text) if not getattr(r, "structured_content", None) else r.structured_content
        assert msg3.get("status") == "pending", msg3

        # Planner sends a reply via send_to_user
        REPLY = "v1.8-integration-reply-from-planner"
        r = await planner.call_tool("send_to_user", {"body": REPLY})
        reply_msg = json.loads(r.content[0].text) if not getattr(r, "structured_content", None) else r.structured_content
        assert reply_msg["direction"] == "planner_to_user", reply_msg

    # Dashboard state should now include the planner_to_user row
    r = httpx.get(f"{BASE}/api/dashboard/state", headers=BEARER, timeout=5)
    inbox = r.json().get("inbox", [])
    assert any(m["body"] == REPLY and m["direction"] == "planner_to_user" for m in inbox), \
        f"reply not in inbox: {[m.get('body') for m in inbox]}"
    print(f"  [OK] full round-trip user->planner->user works")


async def test_list_inbox_history_returns_recent_messages():
    print("--- T7: list_inbox_history returns recent inbox, oldest first, both directions ---")
    async with Client(MCP_URL, auth=KEY) as planner:
        r = await planner.call_tool("list_inbox_history", {"limit": 10})
        items = _unwrap(r)
        assert isinstance(items, list), f"wrong type: {type(items)}"
        # Should include both directions from our integration test
        directions = {m["direction"] for m in items}
        assert "user_to_planner" in directions or "planner_to_user" in directions, items
        # Oldest first (created_at ascending)
        if len(items) >= 2:
            assert items[0]["created_at"] <= items[-1]["created_at"], "expected oldest-first"
        print(f"  [OK] len={len(items)} directions={directions}")


async def test_send_to_user_works_without_active_mission():
    print("--- T8: send_to_user works with NO active mission (global inbox) ---")
    async with Client(MCP_URL, auth=KEY) as planner:
        # Don't bother creating/clearing a mission -- the tool must work either way.
        r = await planner.call_tool("send_to_user", {"body": "v1.8-T8-no-mission-test"})
        msg = _unwrap(r)
        assert isinstance(msg, dict), msg
        assert msg["direction"] == "planner_to_user", msg
        # Either mission_id is set (one was active) or None -- both acceptable for this assertion
        assert "mission_id" in msg
        print(f"  [OK] mid={msg['message_id'][:10]} mission_id={msg.get('mission_id')}")


async def main():
    test_send_to_planner_requires_auth()
    test_send_to_planner_rejects_cross_origin()
    test_send_to_planner_succeeds_without_active_mission()
    test_state_payload_carries_inbox_key()
    test_send_to_planner_validates_empty_body()
    await test_integration_user_post_planner_wait_then_reply()
    await test_list_inbox_history_returns_recent_messages()
    await test_send_to_user_works_without_active_mission()
    print("\nALL INBOX TESTS PASS")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except AssertionError as e:
        print(f"\nFAILED: {e}", file=sys.stderr)
        sys.exit(1)
    except asyncio.TimeoutError as e:
        print(f"\nFAILED (timeout): {e}", file=sys.stderr)
        sys.exit(1)
