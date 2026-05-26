"""v1.12 — get_project_info() MCP tool + project_slug in get_server_info.

Covers:
  T1 — get_project_info on a fresh project returns slug + name + zero counts
  T2 — get_project_info on the default project returns slug="default"
  T3 — get_project_info reports mission_count + active_mission_id correctly
  T4 — get_server_info now includes project_slug field
  T5 — get_project_info on a different project's URL returns that project's data (scope check)

Run against a fresh local server:
    AGENTSHIVE_API_KEY=test-key PORT=8000 AGENTSHIVE_BASE_URL=http://localhost:8000 \
        TOOL_BLOCK_TIMEOUT_SECONDS=10 python -m agentshive.main &
    AGENTSHIVE_BASE=http://localhost:8000 python tests/test_v1_12_project_info.py
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
# client setup, so a copy-paste of `AGENTSHIVE_BASE=<prod>` can't pollute prod.
assert "localhost" in BASE or "127.0.0.1" in BASE, \
    f"refusing to run against non-localhost URL: {BASE}"


def _unwrap(r) -> dict | list:
    sc = r.structured_content if hasattr(r, "structured_content") and r.structured_content is not None else None
    if sc is not None:
        return sc.get("result", sc) if isinstance(sc, dict) else sc
    return json.loads(r.content[0].text)


def _slug() -> str:
    import secrets
    return f"v12-{secrets.token_hex(3)}"


def _post_project(slug: str, name: str, description: str | None = None) -> httpx.Response:
    return httpx.post(
        f"{BASE}/api/dashboard/projects",
        json={"slug": slug, "name": name, "description": description},
        headers=ORIGIN, timeout=5,
    )


def _mcp_url(slug: str | None = None) -> str:
    if slug is None:
        return MCP
    return f"{MCP}?project={slug}"


async def test_get_project_info_fresh_project():
    print("--- T1: get_project_info on a fresh project returns slug + name + zero counts ---")
    s = _slug()
    r = _post_project(s, f"v1.12 Test {s}", description="probe")
    assert r.status_code == 201, r.text[:200]
    async with Client(_mcp_url(s), auth=KEY) as c:
        info = _unwrap(await c.call_tool("get_project_info"))
    assert info["slug"] == s, info
    assert info["name"] == f"v1.12 Test {s}", info
    assert info["description"] == "probe", info
    assert info["mission_count"] == 0, info
    assert info["active_mission_id"] is None, info
    assert info["created_at"] is not None
    assert info["archived_at"] is None
    print(f"  [OK] slug={s}, zero missions, no active")


async def test_get_project_info_default_project():
    print("--- T2: get_project_info on the default project (no ?project=) returns slug=default ---")
    async with Client(MCP, auth=KEY) as c:
        info = _unwrap(await c.call_tool("get_project_info"))
    assert info["slug"] == "default", info
    assert info["name"] is not None, "default project should have a name"
    print(f"  [OK] default slug returned, name={info['name']!r}")


async def test_get_project_info_with_active_mission():
    print("--- T3: get_project_info reports mission_count + active_mission_id ---")
    s = _slug()
    r = _post_project(s, f"v1.12 Active {s}")
    assert r.status_code == 201, r.text[:200]

    async with Client(_mcp_url(s), auth=KEY) as c:
        # Create a mission, then query info
        mission = _unwrap(await c.call_tool("create_mission", {
            "name": "T3 mission",
            "spec": "scaffolding for the mission_count + active_mission_id assertion in T3.",
        }))
        info = _unwrap(await c.call_tool("get_project_info"))

    assert info["slug"] == s, info
    assert info["mission_count"] == 1, info
    assert info["active_mission_id"] == mission["mission_id"], info
    print(f"  [OK] 1 mission, active_mission_id matches")


async def test_get_server_info_includes_project_slug():
    print("--- T4: get_server_info response includes project_slug field ---")
    s = _slug()
    r = _post_project(s, f"v1.12 Info {s}")
    assert r.status_code == 201, r.text[:200]
    async with Client(_mcp_url(s), auth=KEY) as c:
        info = _unwrap(await c.call_tool("get_server_info"))
    assert "project_slug" in info, "get_server_info must include project_slug as of v1.12"
    assert info["project_slug"] == s, info
    # Existing fields still present
    assert "server_version" in info and "tools_catalog_hash" in info and "started_at" in info
    print(f"  [OK] project_slug={s}, existing fields preserved")


async def test_get_project_info_scope_isolation():
    print("--- T5: get_project_info on project A's URL returns A's data, not B's ---")
    a, b = _slug(), _slug()
    _post_project(a, f"v1.12 A {a}")
    _post_project(b, f"v1.12 B {b}")

    async with Client(_mcp_url(a), auth=KEY) as ca:
        info_a = _unwrap(await ca.call_tool("get_project_info"))
    async with Client(_mcp_url(b), auth=KEY) as cb:
        info_b = _unwrap(await cb.call_tool("get_project_info"))

    assert info_a["slug"] == a, info_a
    assert info_b["slug"] == b, info_b
    assert info_a["name"] != info_b["name"]
    print(f"  [OK] A and B returned distinct slugs ({a} != {b})")


async def main():
    await test_get_project_info_fresh_project()
    await test_get_project_info_default_project()
    await test_get_project_info_with_active_mission()
    await test_get_server_info_includes_project_slug()
    await test_get_project_info_scope_isolation()
    print("\nALL v1.12 PROJECT_INFO TESTS PASS")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except AssertionError as e:
        print(f"\nFAILED: {e}", file=sys.stderr)
        sys.exit(1)
    except asyncio.TimeoutError as e:
        print(f"\nFAILED (timeout): {e}", file=sys.stderr)
        sys.exit(1)
