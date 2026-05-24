"""End-to-end smoke test: simulate a full mission lifecycle against a running server.

Requires the server to be running at http://localhost:8000 with AGENTSHIVE_API_KEY=test-key.

Two concurrent "clients":
- Planner: creates a mission, polls for the Coder's question, answers it, responds to summary, marks done
- Coder:   fetches mission, asks a question (blocks), submits a progress summary (blocks),
           checks is_mission_done
"""

import asyncio
import sys

from fastmcp import Client

API_KEY = "test-key"
URL = "http://localhost:8000/mcp"


def _ok(label, result):
    print(f"  [OK] {label}: {result}")


def _content(result):
    """Extract structured content from a CallToolResult."""
    if result.structured_content is not None:
        return result.structured_content
    if result.content:
        return result.content[0].text
    return None


async def planner_role(started: asyncio.Event, mission_id_holder: dict):
    async with Client(URL, auth=API_KEY) as client:
        print("[Planner] creating mission...")
        r = await client.call_tool("create_mission", {
            "name": "Smoke test mission",
            "spec": "Build a hello-world endpoint. Return JSON {'hello':'world'}.",
        })
        m = _content(r)
        mission_id_holder["id"] = m["mission_id"]
        _ok("create_mission", m["mission_id"])
        started.set()

        # Wait for the Coder to ask a question
        for _ in range(30):
            await asyncio.sleep(1)
            r = await client.call_tool("list_pending_questions", {})
            qs = _content(r)
            if qs and (isinstance(qs, dict) and qs.get("result") or isinstance(qs, list) and qs):
                pending = qs if isinstance(qs, list) else qs.get("result", [])
                if pending:
                    qid = pending[0]["question_id"]
                    _ok("got pending question", qid)
                    r = await client.call_tool("answer_question", {
                        "question_id": qid,
                        "answer": "Use Flask. Bind to port 8080.",
                    })
                    _ok("answer_question", _content(r).get("answered_at"))
                    break
        else:
            raise SystemExit("[Planner] never saw a pending question")

        # Wait for a progress summary
        for _ in range(30):
            await asyncio.sleep(1)
            r = await client.call_tool("list_pending_summaries", {})
            ss = _content(r)
            if ss and (isinstance(ss, list) and ss or isinstance(ss, dict) and ss.get("result")):
                pending = ss if isinstance(ss, list) else ss.get("result", [])
                if pending:
                    sid = pending[0]["summary_id"]
                    _ok("got pending summary", sid)
                    r = await client.call_tool("respond_to_summary", {
                        "summary_id": sid,
                        "response": "Looks good — ship it.",
                    })
                    _ok("respond_to_summary", _content(r).get("responded_at"))
                    break
        else:
            raise SystemExit("[Planner] never saw a pending summary")

        # Mark done
        r = await client.call_tool("mark_mission_done", {})
        _ok("mark_mission_done", _content(r).get("status"))


async def coder_role(started: asyncio.Event):
    await started.wait()
    async with Client(URL, auth=API_KEY) as client:
        r = await client.call_tool("fetch_mission", {})
        m = _content(r)
        _ok("fetch_mission", m.get("name"))

        print("[Coder] asking planner a question (will block until planner answers)...")
        r = await client.call_tool("ask_planner", {
            "question": "Should I use Flask or FastAPI? And which port?",
        })
        a = _content(r)
        _ok("ask_planner answered", a.get("answer"))

        print("[Coder] submitting progress summary (will block until planner responds)...")
        r = await client.call_tool("submit_progress", {
            "summary": "Implemented the hello-world endpoint using Flask on port 8080. Returns {'hello':'world'}.",
        })
        a = _content(r)
        _ok("submit_progress responded", a.get("response"))

        r = await client.call_tool("is_mission_done", {})
        _ok("is_mission_done", _content(r).get("done"))


async def main():
    started = asyncio.Event()
    mission_id_holder = {}
    await asyncio.gather(
        planner_role(started, mission_id_holder),
        coder_role(started),
    )
    print("\nALL OK")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:
        print(f"\nFAILED: {type(e).__name__}: {e}", file=sys.stderr)
        raise
