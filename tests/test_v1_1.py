"""v1.1 feature coverage: planner long-poll, bidirectional messaging, coder heartbeat.

Run against a fresh local server:
    AGENTSHIVE_API_KEY=test-key PORT=8001 TOOL_BLOCK_TIMEOUT_SECONDS=8 \
        python -m agentshive.main &
    python tests/test_v1_1.py

Tests intentionally use a short server-side block timeout (8s via env var) so the
timeout-path assertions complete quickly.
"""

import asyncio
import os
import sys

from fastmcp import Client

KEY = os.environ.get("AGENTSHIVE_API_KEY", "test-key")
URL = os.environ.get("AGENTSHIVE_URL", "http://localhost:8001/mcp")


def _c(r):
    return r.structured_content if r.structured_content is not None else (r.content[0].text if r.content else None)


# ---------- FEATURE 1: Planner long-poll ----------

async def test_long_poll_timeout():
    print("--- F1.a: wait_for_next_question with no items returns pending ---")
    async with Client(URL, auth=KEY) as cli:
        r = _c(await cli.call_tool("wait_for_next_question", {"timeout_seconds": 3}))
        assert r.get("status") == "pending", f"expected pending, got {r}"
        print("  [OK] timeout path returns {status: pending}")


async def test_long_poll_catches_question():
    print("--- F1.b: wait_for_next_question catches a question pushed mid-wait ---")
    async with Client(URL, auth=KEY) as cli:
        await cli.call_tool("create_mission", {"name": "lp-q", "spec": "s"})

        async def coder_ask():
            async with Client(URL, auth=KEY) as cli2:
                await cli2.call_tool("ask_planner", {"question": "use foo or bar?"})

        asker = asyncio.create_task(coder_ask())
        await asyncio.sleep(0.5)
        r = _c(await cli.call_tool("wait_for_next_question", {"timeout_seconds": 8}))
        assert r.get("body") == "use foo or bar?", f"expected the pushed question, got {r}"
        await cli.call_tool("answer_question", {"question_id": r["question_id"], "answer": "foo"})
        await asker
        print(f"  [OK] long-poll returned question_id={r['question_id'][:8]}")


async def test_long_poll_catches_summary():
    print("--- F1.c: wait_for_next_summary catches a summary pushed mid-wait ---")
    async with Client(URL, auth=KEY) as cli:
        await cli.call_tool("create_mission", {"name": "lp-s", "spec": "s"})

        async def coder_submit():
            async with Client(URL, auth=KEY) as cli2:
                await cli2.call_tool("submit_progress", {"summary": "done step 1"})

        submitter = asyncio.create_task(coder_submit())
        await asyncio.sleep(0.5)
        r = _c(await cli.call_tool("wait_for_next_summary", {"timeout_seconds": 8}))
        assert r.get("body") == "done step 1", f"expected the pushed summary, got {r}"
        await cli.call_tool("respond_to_summary", {"summary_id": r["summary_id"], "response": "ok"})
        await submitter
        print(f"  [OK] long-poll returned summary_id={r['summary_id'][:8]}")


async def test_long_poll_survives_supersede():
    print("--- F1.d: long-poll survives a supersede; returns when new active mission gets a question ---")
    async with Client(URL, auth=KEY) as cli:
        await cli.call_tool("create_mission", {"name": "lp-X", "spec": "X"})

        async def supersede_and_ask():
            await asyncio.sleep(1.0)
            async with Client(URL, auth=KEY) as cli2:
                await cli2.call_tool("create_mission", {"name": "lp-Y", "spec": "Y"})

                async def ask_on_y():
                    async with Client(URL, auth=KEY) as cli3:
                        await cli3.call_tool("ask_planner", {"question": "fresh on Y"})

                asyncio.create_task(ask_on_y())

        asyncio.create_task(supersede_and_ask())
        r = _c(await cli.call_tool("wait_for_next_question", {"timeout_seconds": 10}))
        assert r.get("body") == "fresh on Y", f"expected fresh-on-Y, got {r}"
        await cli.call_tool("answer_question", {"question_id": r["question_id"], "answer": "k"})
        print("  [OK] long-poll survived supersede and returned a question on the new active mission")


# ---------- FEATURE 2: bidirectional messaging ----------

async def test_planner_to_coder_message():
    print("--- F2.a: send_to_coder + wait_for_planner_message + ack_message ---")
    async with Client(URL, auth=KEY) as cli:
        await cli.call_tool("create_mission", {"name": "msg-pc", "spec": "s"})
        sent = _c(await cli.call_tool("send_to_coder", {"body": "hey heads up: rate-limit is 100/s"}))
        assert sent["direction"] == "planner_to_coder", f"wrong direction: {sent}"
        assert sent["delivered_at"] is None, f"new message should be unacked: {sent}"
        print(f"  [OK] send_to_coder created message_id={sent['message_id'][:8]}")

        async with Client(URL, auth=KEY) as coder:
            # v1.2: wait_for_*_message no longer auto-stamps; ack_message is required
            r = _c(await coder.call_tool("wait_for_planner_message", {"timeout_seconds": 5}))
            assert r["body"] == "hey heads up: rate-limit is 100/s", f"unexpected body: {r}"
            assert r["delivered_at"] is None, f"wait should NOT auto-stamp (v1.2): {r}"
            acked = _c(await coder.call_tool("ack_message", {"message_id": r["message_id"]}))
            assert acked["delivered_at"] is not None, f"ack should stamp: {acked}"
            print("  [OK] Coder received unacked; ack_message stamped delivered_at")


async def test_coder_to_planner_message():
    print("--- F2.b: send_to_planner + wait_for_coder_message + ack ---")
    async with Client(URL, auth=KEY) as cli:
        await cli.call_tool("create_mission", {"name": "msg-cp", "spec": "s"})

        async with Client(URL, auth=KEY) as coder:
            sent = _c(await coder.call_tool("send_to_planner", {"body": "fyi: noticed FastMCP wraps lists"}))
            assert sent["direction"] == "coder_to_planner", f"wrong direction: {sent}"
            print(f"  [OK] send_to_planner created message_id={sent['message_id'][:8]}")

        r = _c(await cli.call_tool("wait_for_coder_message", {"timeout_seconds": 5}))
        assert r["body"] == "fyi: noticed FastMCP wraps lists", f"unexpected body: {r}"
        assert r["delivered_at"] is None, f"wait should NOT auto-stamp: {r}"
        acked = _c(await cli.call_tool("ack_message", {"message_id": r["message_id"]}))
        assert acked["delivered_at"] is not None, f"ack should stamp: {acked}"
        print("  [OK] Planner received unacked; ack stamped")


async def test_message_drain_in_order():
    print("--- F2.c: drain multiple in order (with ack between each) ---")
    async with Client(URL, auth=KEY) as cli:
        await cli.call_tool("create_mission", {"name": "msg-drain", "spec": "s"})
        for body in ["m1", "m2", "m3"]:
            await cli.call_tool("send_to_coder", {"body": body})

        async with Client(URL, auth=KEY) as coder:
            seen = []
            for _ in range(3):
                r = _c(await coder.call_tool("wait_for_planner_message", {"timeout_seconds": 3}))
                seen.append(r["body"])
                # ack between each wait — without this, v1.2 would return m1 every time
                await coder.call_tool("ack_message", {"message_id": r["message_id"]})
            assert seen == ["m1", "m2", "m3"], f"expected ordered drain, got {seen}"
            # Now the queue is empty — next call should timeout
            r = _c(await coder.call_tool("wait_for_planner_message", {"timeout_seconds": 2}))
            assert r.get("status") == "pending", f"queue should be drained, got {r}"
        print("  [OK] drained 3 messages with ack between; subsequent wait timed out")


async def test_message_timeout_path():
    print("--- F2.d: wait_for_*_message timeout returns pending ---")
    async with Client(URL, auth=KEY) as cli:
        r = _c(await cli.call_tool("wait_for_coder_message", {"timeout_seconds": 2}))
        assert r.get("status") == "pending", f"expected pending, got {r}"
        print("  [OK]")


# ---------- FEATURE 3: coder heartbeat (already partially covered in F2.a/F2.b) ----------

async def test_heartbeat_bumps_on_every_coder_call():
    print("--- F3.a: coder_last_seen bumps on every Coder-side call ---")
    async with Client(URL, auth=KEY) as cli:
        # Fresh mission
        r = _c(await cli.call_tool("create_mission", {"name": "hb-all", "spec": "s"}))
        assert r["coder_last_seen"] is None, f"fresh mission should be null, got {r}"

        # Each Coder call below should set/bump coder_last_seen
        last = None
        coder_calls = [
            ("fetch_mission", {}),
            ("is_mission_done", {}),
            ("send_to_planner", {"body": "hb test 1"}),
        ]
        for tool, args in coder_calls:
            await asyncio.sleep(1.05)
            await cli.call_tool(tool, args)
            r = _c(await cli.call_tool("is_mission_done", {}))
            cls = r["mission"]["coder_last_seen"]
            assert cls is not None, f"{tool} should have set coder_last_seen"
            if last is not None:
                assert cls > last, f"{tool} did not bump heartbeat: {last} -> {cls}"
            last = cls
            print(f"  [OK] {tool:24s} bumped heartbeat to {cls[14:19]}")


async def test_heartbeat_NOT_bumped_by_planner_calls():
    print("--- F3.b: Planner-side calls do NOT bump coder_last_seen ---")
    async with Client(URL, auth=KEY) as cli:
        await cli.call_tool("create_mission", {"name": "hb-planner", "spec": "s"})
        await cli.call_tool("fetch_mission", {})  # set initial heartbeat as Coder
        r = _c(await cli.call_tool("is_mission_done", {}))
        baseline = r["mission"]["coder_last_seen"]

        # Planner-side calls — none should bump
        await asyncio.sleep(1.05)
        await cli.call_tool("list_pending_questions", {})
        await cli.call_tool("list_pending_summaries", {})
        await cli.call_tool("get_active_mission", {})

        # Read back via list_pending_summaries which doesn't touch heartbeat
        # (use a direct DB-style read: get_active_mission also doesn't touch)
        r = _c(await cli.call_tool("get_active_mission", {}))
        after = r["coder_last_seen"]
        assert after == baseline, f"Planner calls bumped heartbeat: {baseline} -> {after}"
        print(f"  [OK] coder_last_seen unchanged after 3 Planner-side calls")


# ---------- FEATURE 4: timeout default and list docs (lightweight) ----------

async def test_default_timeout_bumped():
    print("--- F4.a: TOOL_BLOCK_TIMEOUT_SECONDS default is 240 ---")
    from agentshive.config import load_settings
    # In tests we override via env, so just verify the default value via direct import
    saved = os.environ.pop("TOOL_BLOCK_TIMEOUT_SECONDS", None)
    try:
        # Need API key to load settings; use whatever's in env or fallback
        os.environ.setdefault("AGENTSHIVE_API_KEY", "default-check")
        s = load_settings()
        assert s.tool_block_timeout_seconds == 240.0, f"expected default 240, got {s.tool_block_timeout_seconds}"
        print(f"  [OK] config default is {s.tool_block_timeout_seconds}s")
    finally:
        if saved is not None:
            os.environ["TOOL_BLOCK_TIMEOUT_SECONDS"] = saved


async def main():
    # Run sequentially; each test creates its own mission so state is isolated
    await test_long_poll_timeout()
    await test_long_poll_catches_question()
    await test_long_poll_catches_summary()
    await test_long_poll_survives_supersede()
    await test_planner_to_coder_message()
    await test_coder_to_planner_message()
    await test_message_drain_in_order()
    await test_message_timeout_path()
    await test_heartbeat_bumps_on_every_coder_call()
    await test_heartbeat_NOT_bumped_by_planner_calls()
    await test_default_timeout_bumped()
    print("\nALL v1.1 TESTS PASS")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except AssertionError as e:
        print(f"\nFAILED: {e}", file=sys.stderr)
        sys.exit(1)
