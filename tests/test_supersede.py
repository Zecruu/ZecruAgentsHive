"""Regression tests for the supersede-stranding bug fix.

Run against a fresh local server on port 8001:
    AGENTSHIVE_API_KEY=test-key PORT=8001 TOOL_BLOCK_TIMEOUT_SECONDS=20 \
        python -m agentshive.main &
    python tests/test_supersede.py

Covers three scenarios:
1. Base end-to-end flow (regression): every existing tool still behaves as before, and
   is_mission_done now additionally returns a `status` field carrying the literal
   mission.status string.
2. Supersede unblocks a Coder waiting on ask_planner: if the Planner calls create_mission
   while the Coder is blocked, wait_for_answer must return {status: "superseded", ...}
   instead of looping forever.
3. mark_mission_done unblocks a Coder waiting on submit_progress: same idea -- the Coder
   gets {status: "done", ...} and can stop.
"""

import asyncio
import os
import sys

from fastmcp import Client

KEY = os.environ.get("AGENTSHIVE_API_KEY", "test-key")
URL = os.environ.get("AGENTSHIVE_URL", "http://localhost:8000/mcp")


def _content(r):
    return r.structured_content if r.structured_content is not None else (r.content[0].text if r.content else None)


async def test_base_flow():
    print("--- TEST 1: base end-to-end flow (regression) ---")
    started = asyncio.Event()

    async def planner():
        async with Client(URL, auth=KEY) as cli:
            await cli.call_tool("create_mission", {"name": "base", "spec": "hello"})
            started.set()
            for _ in range(15):
                await asyncio.sleep(1)
                qs = _content(await cli.call_tool("list_pending_questions", {}))
                qs = qs.get("result", []) if isinstance(qs, dict) else qs
                if qs:
                    await cli.call_tool("answer_question", {"question_id": qs[0]["question_id"], "answer": "go ahead"})
                    break
            for _ in range(15):
                await asyncio.sleep(1)
                ss = _content(await cli.call_tool("list_pending_summaries", {}))
                ss = ss.get("result", []) if isinstance(ss, dict) else ss
                if ss:
                    await cli.call_tool("respond_to_summary", {"summary_id": ss[0]["summary_id"], "response": "ship"})
                    break
            await cli.call_tool("mark_mission_done", {})

    async def coder():
        await started.wait()
        async with Client(URL, auth=KEY) as cli:
            assert _content(await cli.call_tool("fetch_mission", {}))["name"] == "base"
            ans = _content(await cli.call_tool("ask_planner", {"question": "q?"}))
            assert ans.get("status") == "answered", f"expected answered, got {ans}"
            resp = _content(await cli.call_tool("submit_progress", {"summary": "done"}))
            assert resp.get("status") == "responded", f"expected responded, got {resp}"
            d = _content(await cli.call_tool("is_mission_done", {}))
            assert d["done"] is True and d["status"] == "done", f"expected done, got {d}"

    await asyncio.gather(planner(), coder())
    print('  [OK] base flow passes; is_mission_done returns status="done"')


async def test_supersede_unblocks_coder():
    print("--- TEST 2: supersede unblocks Coder blocked on ask_planner ---")
    async with Client(URL, auth=KEY) as cli:
        r = await cli.call_tool("create_mission", {"name": "mission-A", "spec": "A"})
        mid_a = _content(r)["mission_id"]

        q = _content(await cli.call_tool("ask_planner", {"question": "will block -- no one will answer"}))
        assert q.get("status") == "pending", f"expected pending, got {q}"
        qid = q["question_id"]

        await cli.call_tool("create_mission", {"name": "mission-B", "spec": "B"})

        r2 = _content(await cli.call_tool("wait_for_answer", {"question_id": qid}))
        assert r2.get("status") == "superseded", f"expected superseded, got {r2}"
        assert r2.get("mission_id") == mid_a, f"wrong mission_id: {r2}"
        print(f"  [OK] wait_for_answer returned status=superseded")

        s = _content(await cli.call_tool("is_mission_done", {"mission_id": mid_a}))
        assert s["status"] == "superseded" and s["done"] is False, f"expected superseded/done=False, got {s}"
        print(f"  [OK] is_mission_done(mission_id=A) reports status=superseded")

        s2 = _content(await cli.call_tool("is_mission_done", {}))
        assert s2["status"] == "active" and s2["done"] is False, f"expected active mission B, got {s2}"
        print(f"  [OK] is_mission_done() with no arg reports active mission B")


async def test_done_unblocks_coder():
    print("--- TEST 3: mark_mission_done unblocks Coder blocked on submit_progress ---")
    async with Client(URL, auth=KEY) as cli:
        await cli.call_tool("create_mission", {"name": "mission-C", "spec": "C"})
        s = _content(await cli.call_tool("submit_progress", {"summary": "will block on response"}))
        assert s.get("status") == "pending", f"expected pending, got {s}"
        sid = s["summary_id"]

        await cli.call_tool("mark_mission_done", {})

        r = _content(await cli.call_tool("wait_for_summary_response", {"summary_id": sid}))
        assert r.get("status") == "done", f"expected status=done, got {r}"
        print(f"  [OK] wait_for_summary_response returned status=done")


async def main():
    await test_base_flow()
    await test_supersede_unblocks_coder()
    await test_done_unblocks_coder()
    print("\nALL TESTS PASS")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except AssertionError as e:
        print(f"\nFAILED: {e}", file=sys.stderr)
        sys.exit(1)
