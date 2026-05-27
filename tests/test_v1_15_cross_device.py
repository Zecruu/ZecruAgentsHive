"""v1.15 -- os_hint on CoderHeartbeat + tool params + dashboard surface.

Covers:
  T1 -- os_hint persists on CoderHeartbeat row when passed to ask_planner
  T2 -- invalid os_hint rejected by validation (returns error, no DB write)
  T3 -- missing os_hint is fine (None stored, no error)
  T4 -- os_hint surfaces in /api/dashboard/state connected_coders payload
  T5 -- legacy Coder (no coder_id, no os_hint) still works (backwards compat)

Run against a fresh local server:
    AGENTSHIVE_API_KEY=test-key PORT=8000 AGENTSHIVE_BASE_URL=http://localhost:8000 \\
        TOOL_BLOCK_TIMEOUT_SECONDS=10 python -m agentshive.main &
    AGENTSHIVE_BASE=http://localhost:8000 python tests/test_v1_15_cross_device.py
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
    return f"v15-{secrets.token_hex(3)}"


def _post_project(slug: str, name: str) -> httpx.Response:
    return httpx.post(
        f"{BASE}/api/dashboard/projects",
        json={"slug": slug, "name": name},
        headers=ORIGIN, timeout=5,
    )


def _mcp_url(slug: str) -> str:
    return f"{MCP}?project={slug}"


def _state(slug: str) -> dict:
    """Fetch dashboard state for a specific project (bearer auth)."""
    r = httpx.get(f"{BASE}/api/dashboard/state?project={slug}", headers=BEARER, timeout=5)
    assert r.status_code == 200, r.text[:200]
    return r.json()


async def _create_mission(slug: str, name: str, spec: str) -> dict:
    async with Client(_mcp_url(slug), auth=KEY) as c:
        return _unwrap(await c.call_tool(
            "create_mission", {"name": name, "spec": spec},
        ))


async def test_os_hint_persists_on_heartbeat():
    print("--- T1: os_hint persists on CoderHeartbeat when passed via ask_planner ---")
    s = _slug()
    _post_project(s, f"v1.15 T1 {s}")
    await _create_mission(s, "T1 mission", "scaffolding for T1.")

    async with Client(_mcp_url(s), auth=KEY) as c:
        # ask_planner with coder_id + os_hint. Don't wait for an answer; we
        # only need the side-effect of _touch_coder writing CoderHeartbeat.
        # We catch the pending response.
        try:
            r = await c.call_tool("ask_planner", {
                "question": "T1 question",
                "coder_id": "coder-t1",
                "os_hint": "windows",
            })
        except Exception:
            # Long-poll inside ask_planner may time out; the persistence
            # side-effect runs first, so we proceed regardless.
            pass

    state = _state(s)
    coders = state.get("connected_coders", [])
    match = next((c for c in coders if c["coder_id"] == "coder-t1"), None)
    assert match is not None, f"coder-t1 not in connected_coders: {coders}"
    assert match.get("os_hint") == "windows", f"expected os_hint=windows, got {match!r}"
    print(f"  [OK] os_hint=windows persisted")


async def test_invalid_os_hint_rejected():
    print("--- T2: invalid os_hint rejected ---")
    s = _slug()
    _post_project(s, f"v1.15 T2 {s}")
    await _create_mission(s, "T2 mission", "scaffolding for T2.")

    async with Client(_mcp_url(s), auth=KEY) as c:
        # The tool returns {"error": ...} in its result envelope for validation
        # errors (not an exception). Check both error pathways.
        r = await c.call_tool("submit_progress", {
            "summary": "T2 summary",
            "coder_id": "coder-t2",
            "os_hint": "Windows",   # capitalized -- should be rejected (strict allow-list)
        })
        result = _unwrap(r)
    # Validation error returns either {"error": "..."} dict or empty list result
    err_text = json.dumps(result)
    assert "os_hint" in err_text, f"expected os_hint validation error, got {result!r}"
    print(f"  [OK] invalid os_hint rejected")


async def test_missing_os_hint_is_fine():
    print("--- T3: missing os_hint is fine (None stored) ---")
    s = _slug()
    _post_project(s, f"v1.15 T3 {s}")
    await _create_mission(s, "T3 mission", "scaffolding for T3.")

    async with Client(_mcp_url(s), auth=KEY) as c:
        try:
            await c.call_tool("ask_planner", {
                "question": "T3 question",
                "coder_id": "coder-t3",
                # no os_hint
            })
        except Exception:
            pass

    state = _state(s)
    coders = state.get("connected_coders", [])
    match = next((c for c in coders if c["coder_id"] == "coder-t3"), None)
    assert match is not None, f"coder-t3 not in connected_coders: {coders}"
    assert match.get("os_hint") is None, f"expected os_hint=None, got {match!r}"
    print(f"  [OK] os_hint=None for coder without hint")


async def test_os_hint_surfaces_for_macos_and_linux():
    print("--- T4: macos + linux os_hints surface correctly ---")
    s = _slug()
    _post_project(s, f"v1.15 T4 {s}")
    await _create_mission(s, "T4 mission", "scaffolding for T4 multi-OS.")

    async with Client(_mcp_url(s), auth=KEY) as c:
        for cid, os_hint in [("coder-mac", "macos"), ("coder-lin", "linux")]:
            try:
                await c.call_tool("send_to_planner", {
                    "body": f"hi from {os_hint}",
                    "coder_id": cid,
                    "os_hint": os_hint,
                })
            except Exception:
                pass

    state = _state(s)
    coders = {c["coder_id"]: c for c in state.get("connected_coders", [])}
    assert coders.get("coder-mac", {}).get("os_hint") == "macos", coders
    assert coders.get("coder-lin", {}).get("os_hint") == "linux", coders
    print(f"  [OK] macos + linux surfaced")


async def test_legacy_coder_still_works():
    print("--- T5: legacy Coder (no coder_id, no os_hint) still works ---")
    s = _slug()
    _post_project(s, f"v1.15 T5 {s}")
    await _create_mission(s, "T5 mission", "scaffolding for T5 legacy.")

    async with Client(_mcp_url(s), auth=KEY) as c:
        # Call ask_planner with NO coder_id and NO os_hint -- pure v1.10-style
        # legacy call. Must not raise; must not write a CoderHeartbeat row;
        # must still bump Mission.coder_last_seen.
        try:
            await c.call_tool("ask_planner", {"question": "T5 legacy"})
        except Exception:
            pass

    state = _state(s)
    # No CoderHeartbeat row for the legacy call, so connected_coders may be
    # empty OR contain only the <unidentified> bucket from the Question row.
    coders = state.get("connected_coders", [])
    for c in coders:
        # No legacy entry should have a real coder_id; if any does, it's
        # leak from another test (shouldn't happen with per-test projects).
        assert c["coder_id"] in ("<unidentified>",), f"unexpected coder: {c!r}"
    # Mission.coder_last_seen should still tick (heartbeat surface)
    hb = state.get("coder_heartbeat")
    assert hb is not None and hb.get("last_seen") is not None, \
        f"legacy heartbeat not bumped: {hb!r}"
    print(f"  [OK] legacy path preserved (heartbeat ticked, no CoderHeartbeat row)")


async def main():
    await test_os_hint_persists_on_heartbeat()
    await test_invalid_os_hint_rejected()
    await test_missing_os_hint_is_fine()
    await test_os_hint_surfaces_for_macos_and_linux()
    await test_legacy_coder_still_works()
    print("\nALL v1.15 CROSS_DEVICE TESTS PASS")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except AssertionError as e:
        print(f"\nFAILED: {e}", file=sys.stderr)
        sys.exit(1)
    except asyncio.TimeoutError as e:
        print(f"\nFAILED (timeout): {e}", file=sys.stderr)
        sys.exit(1)
