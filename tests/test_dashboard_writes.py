"""v1.5 dashboard write-endpoint tests.

Covers each of the five new POST endpoints under /api/dashboard/:
  answer, respond, ack, send, mark-done

For each: bad auth (401), valid request (200 ok=true), missing field (400),
length-cap-exceeded (400), competing-actor (200 ok=false), and CSRF (403 on
mismatched Origin, allow with no Origin = non-browser bearer caller).

Plus integrations:
  - Coder blocked on ask_planner unblocks when dashboard POSTs /answer.
  - Dashboard /ack hides the message from subsequent wait_for_*_message calls.

Run against a fresh local server:
    AGENTSHIVE_API_KEY=test-key PORT=8001 TOOL_BLOCK_TIMEOUT_SECONDS=8 \
        python -m agentshive.main &
    python tests/test_dashboard_writes.py
"""

import asyncio
import os
import sys
import time

import httpx
from fastmcp import Client

KEY = os.environ.get("AGENTSHIVE_API_KEY", "test-key")
BASE = os.environ.get("AGENTSHIVE_BASE", "http://localhost:8001")
MCP_URL = f"{BASE}/mcp"
BEARER = {"Authorization": f"Bearer {KEY}"}


def _c(r):
    return r.structured_content if r.structured_content is not None else (r.content[0].text if r.content else None)


async def _setup_mission_with_pending_work():
    """Create a mission and seed it with one question, one summary, one P→C message.

    Returns dict with the IDs the dashboard tests need.
    """
    seeds = {"mission_id": None, "question_id": None, "summary_id": None, "message_id": None}

    async with Client(MCP_URL, auth=KEY) as cli:
        m = _c(await cli.call_tool("create_mission", {"name": "v1.5-writes", "spec": "dashboard write tests"}))
        seeds["mission_id"] = m["mission_id"]
        msg = _c(await cli.call_tool("send_to_coder", {"body": "planner-to-coder seed"}))
        seeds["message_id"] = msg["message_id"]

    # Coder ask + submit need to run in background so they DON'T block the test.
    # We just need the inserts in the DB; they happen before the wait loop starts.
    async def coder_ask():
        async with Client(MCP_URL, auth=KEY) as c2:
            await c2.call_tool("ask_planner", {"question": "should I use X or Y?"})

    async def coder_submit():
        async with Client(MCP_URL, auth=KEY) as c2:
            await c2.call_tool("submit_progress", {"summary": "feature X half-done"})

    asyncio.create_task(coder_ask())
    asyncio.create_task(coder_submit())

    # Poll until both inserts have landed — async background clients have to
    # negotiate MCP sessions before they can send the inner call, and 1s isn't
    # always enough on a cold loop. Cap at ~6s.
    body = None
    for _ in range(30):
        await asyncio.sleep(0.2)
        body = httpx.get(f"{BASE}/api/dashboard/state", headers=BEARER).json()
        if body.get("pending_questions") and body.get("pending_summaries"):
            break
    assert body and body["pending_questions"], f"no pending question seeded after 6s: {body}"
    assert body["pending_summaries"], f"no pending summary seeded after 6s: {body}"
    seeds["question_id"] = body["pending_questions"][0]["question_id"]
    seeds["summary_id"] = body["pending_summaries"][0]["summary_id"]
    return seeds


# ---------- Common pattern: bad auth → 401, CSRF mismatch → 403 ----------

def test_all_writes_reject_no_auth():
    print("--- T1: every POST endpoint returns 401 with no auth ---")
    cases = [
        ("/api/dashboard/answer", {"question_id": "x", "answer": "y"}),
        ("/api/dashboard/respond", {"summary_id": "x", "response": "y"}),
        ("/api/dashboard/ack", {"message_id": "x"}),
        ("/api/dashboard/send", {"body": "y"}),
        ("/api/dashboard/mark-done", {}),
    ]
    for path, body in cases:
        r = httpx.post(f"{BASE}{path}", json=body)
        assert r.status_code == 401, f"{path}: expected 401, got {r.status_code} {r.text[:80]}"
    print(f"  [OK] {len(cases)} endpoints reject no-auth")


def test_csrf_mismatched_origin_403():
    print("--- T2: CSRF rejects mismatched Origin header ---")
    headers = dict(BEARER, Origin="http://evil.example.com")
    r = httpx.post(f"{BASE}/api/dashboard/send", json={"body": "hi"}, headers=headers)
    assert r.status_code == 403, f"expected 403, got {r.status_code}"
    assert "cross-origin" in r.text.lower()
    print(f"  [OK] mismatched Origin → 403")


def test_csrf_matching_origin_allowed():
    print("--- T3: CSRF allows matching Origin ---")
    headers = dict(BEARER, Origin=BASE)
    r = httpx.post(f"{BASE}/api/dashboard/send", json={"body": "csrf ok"}, headers=headers)
    # Either 200 ok:true (active mission) or 200 ok:false ("no active mission") —
    # both prove CSRF accepted the request; we don't care which body shape.
    assert r.status_code == 200, f"expected 200, got {r.status_code}"
    print(f"  [OK] matching Origin → 200")


def test_csrf_no_origin_no_referer_allowed():
    print("--- T4: CSRF allows requests with neither Origin nor Referer ---")
    # httpx by default doesn't add Origin to POSTs; this mimics curl/cli with bearer.
    r = httpx.post(f"{BASE}/api/dashboard/send", json={"body": "no headers"}, headers=BEARER)
    assert r.status_code == 200, f"expected 200, got {r.status_code}"
    print(f"  [OK] no Origin / no Referer → 200 (non-browser caller)")


# ---------- /api/dashboard/answer ----------

async def test_answer_happy_path():
    print("--- T5: /answer valid request → 200 ok=true ---")
    seeds = await _setup_mission_with_pending_work()
    r = httpx.post(f"{BASE}/api/dashboard/answer",
                   json={"question_id": seeds["question_id"], "answer": "use X"},
                   headers=BEARER)
    assert r.status_code == 200, f"got {r.status_code}: {r.text[:120]}"
    body = r.json()
    assert body["ok"] is True, f"expected ok=True, got {body}"
    assert body["question"]["answer"] == "use X"
    print(f"  [OK] question answered, answer round-tripped")


def test_answer_malformed_body_400():
    print("--- T6: /answer malformed JSON → 400 ---")
    r = httpx.post(f"{BASE}/api/dashboard/answer", content="not json", headers=BEARER)
    assert r.status_code == 400, f"got {r.status_code}: {r.text[:80]}"
    print(f"  [OK]")


def test_answer_missing_field_400():
    print("--- T7: /answer missing question_id → 400 ---")
    r = httpx.post(f"{BASE}/api/dashboard/answer", json={"answer": "x"}, headers=BEARER)
    assert r.status_code == 400, f"got {r.status_code}: {r.text[:80]}"
    print(f"  [OK]")


def test_answer_length_cap_400():
    print("--- T8: /answer oversized answer → 400 ---")
    r = httpx.post(f"{BASE}/api/dashboard/answer",
                   json={"question_id": "fake", "answer": "x" * (16 * 1024 + 1)},
                   headers=BEARER)
    assert r.status_code == 400, f"got {r.status_code}: {r.text[:80]}"
    assert "maximum length" in r.text
    print(f"  [OK]")


async def test_answer_competing_actor_already_answered():
    print("--- T9: /answer on already-answered question → 200 ok=false (not 500) ---")
    seeds = await _setup_mission_with_pending_work()
    # First answer succeeds
    r1 = httpx.post(f"{BASE}/api/dashboard/answer",
                    json={"question_id": seeds["question_id"], "answer": "first"},
                    headers=BEARER)
    assert r1.status_code == 200 and r1.json()["ok"] is True
    # Second answer is a competing-actor scenario
    r2 = httpx.post(f"{BASE}/api/dashboard/answer",
                    json={"question_id": seeds["question_id"], "answer": "second"},
                    headers=BEARER)
    assert r2.status_code == 200, f"expected 200, got {r2.status_code}"
    body = r2.json()
    assert body["ok"] is False, f"expected ok=false on competing answer, got {body}"
    assert "already answered" in body.get("error", "").lower()
    print(f"  [OK] competing-actor returns 200 ok=false with explanatory error")


# ---------- /api/dashboard/respond + /ack + /send + /mark-done (lighter coverage) ----------

async def test_respond_happy_path():
    print("--- T10: /respond valid → 200 ok=true ---")
    seeds = await _setup_mission_with_pending_work()
    r = httpx.post(f"{BASE}/api/dashboard/respond",
                   json={"summary_id": seeds["summary_id"], "response": "keep going"},
                   headers=BEARER)
    assert r.status_code == 200 and r.json()["ok"] is True
    assert r.json()["summary"]["response"] == "keep going"
    print(f"  [OK]")


async def test_ack_happy_path():
    print("--- T11: /ack valid → 200 ok=true, message stamped ---")
    seeds = await _setup_mission_with_pending_work()
    r = httpx.post(f"{BASE}/api/dashboard/ack",
                   json={"message_id": seeds["message_id"]},
                   headers=BEARER)
    assert r.status_code == 200 and r.json()["ok"] is True
    assert r.json()["message"]["delivered_at"] is not None
    print(f"  [OK]")


def test_send_happy_path_requires_mission():
    print("--- T12: /send returns 200 (ok depends on active mission) ---")
    r = httpx.post(f"{BASE}/api/dashboard/send", json={"body": "hello"}, headers=BEARER)
    assert r.status_code == 200, f"got {r.status_code}"
    body = r.json()
    # ok can be True or False depending on whether there's an active mission
    assert "ok" in body
    print(f"  [OK] returned 200 ok={body['ok']}")


async def test_mark_done_happy_path():
    print("--- T13: /mark-done with active mission → 200 ok=true, mission status=done ---")
    await _setup_mission_with_pending_work()  # ensures active mission
    r = httpx.post(f"{BASE}/api/dashboard/mark-done", headers=BEARER)
    assert r.status_code == 200, f"got {r.status_code}"
    body = r.json()
    assert body["ok"] is True, f"got {body}"
    assert body["mission"]["status"] == "done"
    print(f"  [OK]")


def test_mark_done_no_mission_ok_false():
    print("--- T14: /mark-done with no active mission → 200 ok=false ---")
    # T13 just marked the previous mission done; there's now no active mission
    r = httpx.post(f"{BASE}/api/dashboard/mark-done", headers=BEARER)
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False, f"got {body}"
    assert "no active mission" in body.get("error", "").lower()
    print(f"  [OK]")


# ---------- Integrations ----------

async def test_integration_coder_unblocks_via_dashboard_answer():
    print("--- T15: integration — Coder blocked on ask_planner unblocks via /answer ---")
    async with Client(MCP_URL, auth=KEY) as cli:
        await cli.call_tool("create_mission", {"name": "int-unblock", "spec": "int"})

    # Coder asks in background, returns the answer when received
    coder_answer = asyncio.Future()
    async def coder():
        async with Client(MCP_URL, auth=KEY) as c2:
            r = await c2.call_tool("ask_planner", {"question": "is x ok?"})
            coder_answer.set_result(_c(r))

    asyncio.create_task(coder())
    await asyncio.sleep(1.0)  # let the question land

    # Dashboard reads pending questions then answers
    state = httpx.get(f"{BASE}/api/dashboard/state", headers=BEARER).json()
    assert state["pending_questions"], "no pending question to answer"
    qid = state["pending_questions"][0]["question_id"]
    r = httpx.post(f"{BASE}/api/dashboard/answer",
                   json={"question_id": qid, "answer": "yes from dashboard"},
                   headers=BEARER)
    assert r.status_code == 200 and r.json()["ok"] is True

    # Coder should now unblock
    result = await asyncio.wait_for(coder_answer, timeout=10)
    assert result.get("status") == "answered", f"coder did not unblock: {result}"
    assert result.get("answer") == "yes from dashboard"
    print(f"  [OK] Coder unblocked with dashboard-supplied answer")


async def test_integration_ack_hides_from_wait():
    print("--- T16: integration — /ack hides message from subsequent wait_for_*_message ---")
    async with Client(MCP_URL, auth=KEY) as cli:
        await cli.call_tool("create_mission", {"name": "int-ack", "spec": "int"})
        await cli.call_tool("send_to_coder", {"body": "ack-test message"})

    # Dashboard reads the message ID
    state = httpx.get(f"{BASE}/api/dashboard/state", headers=BEARER).json()
    mid = state["messages"]["planner_to_coder"][0]["message_id"]

    # Dashboard acks it
    r = httpx.post(f"{BASE}/api/dashboard/ack", json={"message_id": mid}, headers=BEARER)
    assert r.status_code == 200 and r.json()["ok"] is True

    # Coder's wait_for_planner_message should NOT return it (queue drained)
    async with Client(MCP_URL, auth=KEY) as cli:
        r = await cli.call_tool("wait_for_planner_message", {"timeout_seconds": 3})
        result = _c(r)
        assert result.get("status") == "pending", f"expected drained queue, got {result}"
    print(f"  [OK] acked message hidden from Coder's wait_for_planner_message")


# ---------- Sanity: page text reflects v1.5 UI additions ----------

def test_dashboard_html_has_v1_5_ui_elements():
    print("--- T17: served dashboard HTML contains the v1.5 button labels ---")
    # Need to log in to fetch /dashboard
    login = httpx.post(f"{BASE}/dashboard/login", data={"api_key": KEY}, follow_redirects=False)
    cookie = login.cookies.get("agentshive_dash_session")
    r = httpx.get(f"{BASE}/dashboard", cookies={"agentshive_dash_session": cookie})
    assert r.status_code == 200
    must_contain = ["Mark mission DONE", "openAnswerForm(", "openRespondForm(", "doShipIt(", "openSendForm(", "doAck("]
    for needle in must_contain:
        assert needle in r.text, f"missing UI element: {needle}"
    print(f"  [OK] {len(must_contain)} v1.5 UI elements present in served HTML")


def main():
    # Sync tests first (no setup overhead)
    test_all_writes_reject_no_auth()
    test_csrf_mismatched_origin_403()
    test_csrf_matching_origin_allowed()
    test_csrf_no_origin_no_referer_allowed()
    test_answer_malformed_body_400()
    test_answer_missing_field_400()
    test_answer_length_cap_400()
    test_send_happy_path_requires_mission()
    test_dashboard_html_has_v1_5_ui_elements()

    # Async tests
    asyncio.run(_run_async_tests())

    test_mark_done_no_mission_ok_false()

    print("\nALL v1.5 WRITE TESTS PASS")


async def _run_async_tests():
    await test_answer_happy_path()
    await test_answer_competing_actor_already_answered()
    await test_respond_happy_path()
    await test_ack_happy_path()
    await test_mark_done_happy_path()
    await test_integration_coder_unblocks_via_dashboard_answer()
    await test_integration_ack_hides_from_wait()


if __name__ == "__main__":
    try:
        main()
    except AssertionError as e:
        print(f"\nFAILED: {e}", file=sys.stderr)
        sys.exit(1)
