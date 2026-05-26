"""v1.11 per-Coder identity tests.

Covers the four send_to_coder routing paths, coder_id attribution on
questions/summaries/messages, validation, backwards-compat for legacy
Coders (no coder_id), and a migration smoke check.

Run against a fresh local server:
    AGENTSHIVE_API_KEY=test-key PORT=8000 AGENTSHIVE_BASE_URL=http://localhost:8000 \
        TOOL_BLOCK_TIMEOUT_SECONDS=10 python -m agentshive.main &
    AGENTSHIVE_BASE=http://localhost:8000 python tests/test_v1_11_coder_id.py
"""

import asyncio
import json
import os
import secrets
import sys

import httpx
from fastmcp import Client
from sqlalchemy import inspect, text
from sqlmodel import Session

KEY = os.environ.get("AGENTSHIVE_API_KEY", "test-key")
BASE = os.environ.get("AGENTSHIVE_BASE", "http://localhost:8000")
MCP = f"{BASE}/mcp"
BEARER = {"Authorization": f"Bearer {KEY}"}
ORIGIN = {**BEARER, "Origin": BASE}

# v1.8 lesson: guard every test file's main() against accidentally running
# the suite at a non-localhost URL. v1.9 retrofit pass enforced this across
# the entire tests/ tree.
assert "localhost" in BASE or "127.0.0.1" in BASE, \
    f"refusing to run against non-localhost URL: {BASE}"


def _unwrap(r) -> dict | list:
    sc = r.structured_content if hasattr(r, "structured_content") and r.structured_content is not None else None
    if sc is not None:
        return sc.get("result", sc) if isinstance(sc, dict) else sc
    return json.loads(r.content[0].text)


def _slug() -> str:
    return f"t11-{secrets.token_hex(3)}"


def _new_project(slug: str) -> None:
    r = httpx.post(f"{BASE}/api/dashboard/projects",
                   json={"slug": slug, "name": slug}, headers=ORIGIN, timeout=5)
    assert r.status_code == 201, r.text[:200]


async def _open_mission(project_slug: str, name: str = "v1.11 test") -> str:
    async with Client(f"{MCP}?project={project_slug}", auth=KEY) as c:
        d = _unwrap(await c.call_tool("create_mission", {"name": name, "spec": "s"}))
        return d["mission_id"]


# ---------- Coder identity attribution on questions/summaries/messages ----------


async def test_coder_id_on_question():
    print("--- T1: ask_planner(coder_id=A) -> Hivemind sees coder_id on the question ---")
    p = _slug(); _new_project(p)
    await _open_mission(p)
    async with Client(f"{MCP}?project={p}", auth=KEY) as c:
        # ask_planner blocks for the answer -- give it a tiny timeout so it returns
        # status=pending and we can read the question row from list_pending_questions.
        r = await c.call_tool("ask_planner", {"question": "from A?", "coder_id": "coder-a"})
        # We just need the question to have been INSERTED. Pending or answered both fine.
        pending = await c.call_tool("list_pending_questions", {})
        qs = _unwrap(pending)
        assert any(q["coder_id"] == "coder-a" for q in qs), f"missing coder-a: {qs}"
    print("  [OK]")


async def test_distinct_coder_ids_on_questions():
    print("--- T2: Coder A and Coder B asks are attributed distinctly ---")
    p = _slug(); _new_project(p)
    await _open_mission(p)
    async with Client(f"{MCP}?project={p}", auth=KEY) as c:
        await c.call_tool("ask_planner", {"question": "q-from-a", "coder_id": "alpha"})
        await c.call_tool("ask_planner", {"question": "q-from-b", "coder_id": "beta"})
        qs = _unwrap(await c.call_tool("list_pending_questions", {}))
        ids = {q["body"]: q["coder_id"] for q in qs}
        assert ids.get("q-from-a") == "alpha", ids
        assert ids.get("q-from-b") == "beta", ids
    print("  [OK]")


async def test_no_coder_id_legacy_persists_null():
    print("--- T3: legacy Coder (no coder_id) -> row.coder_id stays null ---")
    p = _slug(); _new_project(p)
    await _open_mission(p)
    async with Client(f"{MCP}?project={p}", auth=KEY) as c:
        await c.call_tool("ask_planner", {"question": "legacy"})
        qs = _unwrap(await c.call_tool("list_pending_questions", {}))
        legacy = [q for q in qs if q["body"] == "legacy"]
        assert legacy and legacy[0]["coder_id"] is None, legacy
    print("  [OK]")


async def test_submit_progress_persists_coder_id():
    print("--- T4: submit_progress(coder_id=A) -> coder_id on summary row ---")
    p = _slug(); _new_project(p)
    await _open_mission(p)

    async def _coder_submit(sl):
        async with Client(f"{MCP}?project={sl}", auth=KEY) as c:
            # submit_progress blocks until Planner responds; we don't want to wait.
            # Use a very short timeout via timeout_seconds -- but submit_progress
            # doesn't expose one. Instead, call the underlying tool then poll the
            # pending-summaries list (the row is inserted before the wait loop).
            await c.call_tool("submit_progress", {"summary": "phase-1", "coder_id": "phase-coder"})

    # Run the submit in background; the row appears before the wait completes.
    task = asyncio.create_task(_coder_submit(p))
    # Give the insert a moment to land
    await asyncio.sleep(0.5)
    async with Client(f"{MCP}?project={p}", auth=KEY) as c:
        ss = _unwrap(await c.call_tool("list_pending_summaries", {}))
        match = [s for s in ss if s["body"] == "phase-1"]
        assert match and match[0]["coder_id"] == "phase-coder", match
        # respond so the background submit_progress unblocks
        await c.call_tool("respond_to_summary", {"summary_id": match[0]["summary_id"], "response": "ok"})
    await task
    print("  [OK]")


async def test_invalid_coder_id_rejected():
    print("--- T5: invalid coder_id (uppercase, special chars, too long) -> error ---")
    p = _slug(); _new_project(p)
    await _open_mission(p)
    async with Client(f"{MCP}?project={p}", auth=KEY) as c:
        for bad in ("Coder #1", "a" * 50, "WITH-CAPS", "-foo", "foo-", "foo.bar"):
            r = _unwrap(await c.call_tool("ask_planner", {"question": "?", "coder_id": bad}))
            assert "error" in r and "coder_id must match" in r["error"], (bad, r)
    print("  [OK] 6 bad ids all rejected with validation error")


# ---------- send_to_coder routing matrix (4 paths) ----------


async def _post_planner_message(project_slug: str, body: str, target_coder_id: str | None) -> str:
    """Planner-side send_to_coder, called via the same MCP URL the Coder uses
    (the tool itself doesn't differentiate by role -- it's by convention)."""
    payload = {"body": body}
    if target_coder_id is not None:
        payload["target_coder_id"] = target_coder_id
    async with Client(f"{MCP}?project={project_slug}", auth=KEY) as c:
        r = _unwrap(await c.call_tool("send_to_coder", payload))
        return r["message_id"]


async def test_broadcast_delivered_to_legacy_coder():
    print("--- T6: target=None + coder_id=None (legacy Coder) -> DELIVERED ---")
    p = _slug(); _new_project(p)
    await _open_mission(p)
    await _post_planner_message(p, "everyone-hear-this", target_coder_id=None)
    async with Client(f"{MCP}?project={p}", auth=KEY) as c:
        r = _unwrap(await c.call_tool("wait_for_planner_message", {"timeout_seconds": 3}))
        assert r.get("body") == "everyone-hear-this", r
        assert r["target_coder_id"] is None
        await c.call_tool("ack_message", {"message_id": r["message_id"]})
    print("  [OK]")


async def test_broadcast_delivered_to_identified_coder():
    print("--- T7: target=None + coder_id=A -> DELIVERED ---")
    p = _slug(); _new_project(p)
    await _open_mission(p)
    await _post_planner_message(p, "broadcast-to-all", target_coder_id=None)
    async with Client(f"{MCP}?project={p}", auth=KEY) as c:
        r = _unwrap(await c.call_tool("wait_for_planner_message",
                                       {"timeout_seconds": 3, "coder_id": "a"}))
        assert r.get("body") == "broadcast-to-all", r
        await c.call_tool("ack_message", {"message_id": r["message_id"]})
    print("  [OK]")


async def test_targeted_delivered_to_matching_coder():
    print("--- T8: target=A + coder_id=A -> DELIVERED ---")
    p = _slug(); _new_project(p)
    await _open_mission(p)
    await _post_planner_message(p, "for-a-only", target_coder_id="a")
    async with Client(f"{MCP}?project={p}", auth=KEY) as c:
        r = _unwrap(await c.call_tool("wait_for_planner_message",
                                       {"timeout_seconds": 3, "coder_id": "a"}))
        assert r.get("body") == "for-a-only", r
        assert r["target_coder_id"] == "a"
        await c.call_tool("ack_message", {"message_id": r["message_id"]})
    print("  [OK]")


async def test_targeted_not_delivered_to_other_coder():
    print("--- T9: target=A + coder_id=B -> NOT DELIVERED ---")
    p = _slug(); _new_project(p)
    await _open_mission(p)
    await _post_planner_message(p, "for-a-only", target_coder_id="a")
    async with Client(f"{MCP}?project={p}", auth=KEY) as c:
        r = _unwrap(await c.call_tool("wait_for_planner_message",
                                       {"timeout_seconds": 2, "coder_id": "b"}))
        assert r.get("status") == "pending", f"Coder B leaked A's targeted msg: {r}"
    print("  [OK]")


async def test_targeted_not_delivered_to_legacy_coder():
    print("--- T10: target=A + coder_id=None (legacy) -> NOT DELIVERED (safety) ---")
    p = _slug(); _new_project(p)
    await _open_mission(p)
    await _post_planner_message(p, "for-a-only", target_coder_id="a")
    async with Client(f"{MCP}?project={p}", auth=KEY) as c:
        r = _unwrap(await c.call_tool("wait_for_planner_message", {"timeout_seconds": 2}))
        assert r.get("status") == "pending", f"legacy Coder leaked targeted msg: {r}"
    print("  [OK]")


# ---------- Migration smoke ----------


def test_migration_columns_exist():
    print("--- T11: migration adds coder_id columns to question/summary/message ---")
    # Pull the running server's DB connection via its own env. Since this test
    # process imports agentshive directly, we can use the engine that init_engine
    # populates. But the running server has its own engine. We use a side channel:
    # the dashboard JSON includes the columns implicitly (via the dict serializers)
    # -- so an indirect proof is that /api/dashboard/state contains coder_id fields.
    # Direct schema inspection: connect to the same sqlite/postgres file the
    # server uses. For the test harness we assume the default sqlite path.
    from agentshive.config import load_settings
    from agentshive.db import init_engine
    settings = load_settings()
    eng = init_engine(settings)
    ins = inspect(eng)
    for table, expected in [
        ("question", {"coder_id"}),
        ("summary", {"coder_id"}),
        ("message", {"coder_id", "target_coder_id"}),
    ]:
        cols = {c["name"] for c in ins.get_columns(table)}
        missing = expected - cols
        assert not missing, f"{table} missing {missing}; has {cols}"
    print("  [OK] all v1.11 columns present")


async def main():
    await test_coder_id_on_question()
    await test_distinct_coder_ids_on_questions()
    await test_no_coder_id_legacy_persists_null()
    await test_submit_progress_persists_coder_id()
    await test_invalid_coder_id_rejected()
    await test_broadcast_delivered_to_legacy_coder()
    await test_broadcast_delivered_to_identified_coder()
    await test_targeted_delivered_to_matching_coder()
    await test_targeted_not_delivered_to_other_coder()
    await test_targeted_not_delivered_to_legacy_coder()
    test_migration_columns_exist()
    print("\nALL v1.11 TESTS PASS")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except AssertionError as e:
        print(f"\nFAILED: {e}", file=sys.stderr)
        sys.exit(1)
    except asyncio.TimeoutError as e:
        print(f"\nFAILED (timeout): {e}", file=sys.stderr)
        sys.exit(1)
