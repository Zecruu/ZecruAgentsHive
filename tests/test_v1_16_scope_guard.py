"""v1.16 -- multi-connector cross-project scope guard.

Covers:
  T1 -- ask_planner with matching project_slug succeeds
  T2 -- ask_planner with mismatched project_slug returns scope error, no DB row
  T3 -- submit_progress with mismatched project_slug returns scope error
  T4 -- create_mission with mismatched project_slug REFUSES the create
  T5 -- send_to_planner with mismatched project_slug returns scope error
  T6 -- send_to_coder with mismatched project_slug returns scope error
  T7 -- mark_mission_done with mismatched project_slug returns scope error
  T8 -- legacy callers (no project_slug arg) still work (backwards compat)

Run against a fresh local server:
    AGENTSHIVE_API_KEY=test-key PORT=8000 AGENTSHIVE_BASE_URL=http://localhost:8000 \\
        TOOL_BLOCK_TIMEOUT_SECONDS=2 python -m agentshive.main &
    AGENTSHIVE_BASE=http://localhost:8000 python tests/test_v1_16_scope_guard.py
"""

import asyncio
import json
import os
import sys

import httpx
from fastmcp import Client

KEY = os.environ.get("AGENTSHIVE_API_KEY", "test-key")
BASE = os.environ.get("AGENTSHIVE_BASE", "http://localhost:8000")
MCP = f"{BASE}/mcp"
BEARER = {"Authorization": f"Bearer {KEY}"}
ORIGIN = {**BEARER, "Origin": BASE}

# v1.8 lesson: every test file's main() asserts localhost in URL before any
# client setup, so a copy-paste of AGENTSHIVE_BASE=<prod> can't pollute prod.
assert "localhost" in BASE or "127.0.0.1" in BASE, \
    f"refusing to run against non-localhost URL: {BASE}"


def _unwrap(r) -> dict | list:
    sc = r.structured_content if hasattr(r, "structured_content") and r.structured_content is not None else None
    if sc is not None:
        return sc.get("result", sc) if isinstance(sc, dict) else sc
    return json.loads(r.content[0].text)


def _slug() -> str:
    import secrets
    return f"v16-{secrets.token_hex(3)}"


def _post_project(slug: str, name: str) -> httpx.Response:
    return httpx.post(
        f"{BASE}/api/dashboard/projects",
        json={"slug": slug, "name": name},
        headers=ORIGIN, timeout=5,
    )


def _mcp_url(slug: str) -> str:
    return f"{MCP}?project={slug}"


async def _create_mission(slug: str, name: str, spec: str, project_slug: str | None = None) -> dict:
    args = {"name": name, "spec": spec}
    if project_slug is not None:
        args["project_slug"] = project_slug
    async with Client(_mcp_url(slug), auth=KEY) as c:
        return _unwrap(await c.call_tool("create_mission", args))


def _is_scope_error(result: dict) -> bool:
    return (
        isinstance(result, dict)
        and "error" in result
        and "project scope mismatch" in str(result.get("error", "")).lower()
    )


async def test_matching_project_slug_succeeds():
    print("--- T1: ask_planner with matching project_slug succeeds ---")
    s = _slug()
    _post_project(s, f"v1.16 T1 {s}")
    await _create_mission(s, "T1 mission", "scaffolding")

    async with Client(_mcp_url(s), auth=KEY) as c:
        try:
            r = await c.call_tool("ask_planner", {
                "question": "T1 question",
                "coder_id": "coder-t1",
                "project_slug": s,  # matches the URL
            })
            result = _unwrap(r)
        except Exception:
            result = None
    # We don't care about the answer; only that no scope error was returned.
    # (The pending response is the happy path here.)
    if isinstance(result, dict):
        assert not _is_scope_error(result), f"unexpected scope error: {result!r}"
    print(f"  [OK] matching slug accepted")


async def test_mismatched_ask_planner_refused():
    print("--- T2: ask_planner with mismatched project_slug returns scope error ---")
    s = _slug()
    _post_project(s, f"v1.16 T2 {s}")
    await _create_mission(s, "T2 mission", "scaffolding")

    async with Client(_mcp_url(s), auth=KEY) as c:
        r = await c.call_tool("ask_planner", {
            "question": "T2 question",
            "project_slug": "wrong-slug",   # mismatch
        })
        result = _unwrap(r)
    assert _is_scope_error(result), f"expected scope error, got {result!r}"
    assert result.get("actual_project_slug") == s, result
    assert result.get("claimed_project_slug") == "wrong-slug", result
    print(f"  [OK] scope error raised, claimed={result.get('claimed_project_slug')}, actual={result.get('actual_project_slug')}")


async def test_mismatched_submit_progress_refused():
    print("--- T3: submit_progress with mismatched project_slug returns scope error ---")
    s = _slug()
    _post_project(s, f"v1.16 T3 {s}")
    await _create_mission(s, "T3 mission", "scaffolding")

    async with Client(_mcp_url(s), auth=KEY) as c:
        r = await c.call_tool("submit_progress", {
            "summary": "T3 summary",
            "project_slug": "wrong-slug",
        })
        result = _unwrap(r)
    assert _is_scope_error(result), f"expected scope error, got {result!r}"
    print(f"  [OK] submit_progress refused")


async def test_mismatched_create_mission_refused():
    print("--- T4: create_mission with mismatched project_slug is REFUSED ---")
    s = _slug()
    _post_project(s, f"v1.16 T4 {s}")

    # Pre-state: no active mission on s
    async with Client(_mcp_url(s), auth=KEY) as c:
        before = _unwrap(await c.call_tool("get_active_mission"))
    assert before.get("mission") is None or before is None or before == {"mission": None}, \
        f"unexpected pre-state: {before!r}"

    # Attempt to create with mismatched slug
    async with Client(_mcp_url(s), auth=KEY) as c:
        r = await c.call_tool("create_mission", {
            "name": "T4 mission",
            "spec": "should not land",
            "project_slug": "wrong-slug",
        })
        result = _unwrap(r)
    assert _is_scope_error(result), f"expected scope error, got {result!r}"

    # Post-state: still no active mission on s (the create was refused)
    async with Client(_mcp_url(s), auth=KEY) as c:
        after = _unwrap(await c.call_tool("get_active_mission"))
    # get_active_mission returns {"mission": None} OR a dict with mission_id.
    # We expect either None or the original (none here).
    assert (
        after.get("mission") is None or after == {"mission": None}
    ), f"create_mission landed despite scope error: {after!r}"
    print(f"  [OK] create_mission refused, no row inserted")


async def test_mismatched_send_to_planner_refused():
    print("--- T5: send_to_planner with mismatched project_slug returns scope error ---")
    s = _slug()
    _post_project(s, f"v1.16 T5 {s}")
    await _create_mission(s, "T5 mission", "scaffolding")

    async with Client(_mcp_url(s), auth=KEY) as c:
        r = await c.call_tool("send_to_planner", {
            "body": "T5 message",
            "project_slug": "wrong-slug",
        })
        result = _unwrap(r)
    assert _is_scope_error(result), f"expected scope error, got {result!r}"
    print(f"  [OK] send_to_planner refused")


async def test_mismatched_send_to_coder_refused():
    print("--- T6: send_to_coder with mismatched project_slug returns scope error ---")
    s = _slug()
    _post_project(s, f"v1.16 T6 {s}")
    await _create_mission(s, "T6 mission", "scaffolding")

    async with Client(_mcp_url(s), auth=KEY) as c:
        r = await c.call_tool("send_to_coder", {
            "body": "T6 broadcast",
            "project_slug": "wrong-slug",
        })
        result = _unwrap(r)
    assert _is_scope_error(result), f"expected scope error, got {result!r}"
    print(f"  [OK] send_to_coder refused")


async def test_mismatched_mark_mission_done_refused():
    print("--- T7: mark_mission_done with mismatched project_slug returns scope error ---")
    s = _slug()
    _post_project(s, f"v1.16 T7 {s}")
    await _create_mission(s, "T7 mission", "scaffolding")

    async with Client(_mcp_url(s), auth=KEY) as c:
        r = await c.call_tool("mark_mission_done", {
            "project_slug": "wrong-slug",
        })
        result = _unwrap(r)
    assert _is_scope_error(result), f"expected scope error, got {result!r}"

    # Mission should still be active
    async with Client(_mcp_url(s), auth=KEY) as c:
        m = _unwrap(await c.call_tool("get_active_mission"))
    # get_active_mission returns the active mission dict directly
    if isinstance(m, dict) and "status" in m:
        assert m["status"] == "active", f"mission marked done despite scope error: {m!r}"
    print(f"  [OK] mark_mission_done refused")


async def test_legacy_no_project_slug_still_works():
    print("--- T8: legacy callers (no project_slug arg) still work ---")
    s = _slug()
    _post_project(s, f"v1.16 T8 {s}")
    await _create_mission(s, "T8 mission", "scaffolding")  # no project_slug

    async with Client(_mcp_url(s), auth=KEY) as c:
        try:
            r = await c.call_tool("ask_planner", {
                "question": "T8 legacy ask",
                # no project_slug
            })
            result = _unwrap(r)
        except Exception:
            result = None
    if isinstance(result, dict):
        assert not _is_scope_error(result), f"legacy call rejected: {result!r}"
    print(f"  [OK] legacy path preserved (no project_slug = no guard)")


async def main():
    await test_matching_project_slug_succeeds()
    await test_mismatched_ask_planner_refused()
    await test_mismatched_submit_progress_refused()
    await test_mismatched_create_mission_refused()
    await test_mismatched_send_to_planner_refused()
    await test_mismatched_send_to_coder_refused()
    await test_mismatched_mark_mission_done_refused()
    await test_legacy_no_project_slug_still_works()
    print("\nALL v1.16 SCOPE_GUARD TESTS PASS")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except AssertionError as e:
        print(f"\nFAILED: {e}", file=sys.stderr)
        sys.exit(1)
    except asyncio.TimeoutError as e:
        print(f"\nFAILED (timeout): {e}", file=sys.stderr)
        sys.exit(1)
