"""v1.9 multi-project namespacing tests.

Covers the project lifecycle (CRUD), URL ?project=<slug> routing, ContextVar
plumbing, two-project isolation invariants (missions, inbox, SSE), backward
compat for legacy callers (no ?project= still works), slug validation, and
soft-archive semantics.

Run against a fresh local server:
    AGENTSHIVE_API_KEY=test-key PORT=8000 AGENTSHIVE_BASE_URL=http://localhost:8000 \
        TOOL_BLOCK_TIMEOUT_SECONDS=10 python -m agentshive.main &
    AGENTSHIVE_BASE=http://localhost:8000 python tests/test_projects.py
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
    """Unique kebab slug for each test run so the suite is re-runnable."""
    import secrets
    return f"t-{secrets.token_hex(3)}"


def _post_project(slug: str, name: str, description: str | None = None) -> httpx.Response:
    return httpx.post(f"{BASE}/api/dashboard/projects",
                      json={"slug": slug, "name": name, "description": description},
                      headers=ORIGIN, timeout=5)


def test_create_project_happy_path():
    print("--- T1: POST /api/dashboard/projects creates a project ---")
    s = _slug()
    r = _post_project(s, "Test One")
    assert r.status_code == 201, r.text[:200]
    body = r.json()
    assert body["ok"] is True
    assert body["project"]["slug"] == s
    assert body["project"]["archived_at"] is None
    print(f"  [OK] slug={s}")


def test_default_slug_reserved():
    print("--- T2: 'default' slug is reserved (400) ---")
    r = _post_project("default", "Default Take Two")
    assert r.status_code == 400, r.status_code
    assert "reserved" in r.json()["error"]
    print("  [OK]")


def test_invalid_slug_rejected():
    print("--- T3: invalid slug formats are 400'd ---")
    # Note: the handler auto-lowercases the slug before validating, so 'FooBar'
    # becomes 'foobar' and passes — that's a deliberate UX choice. The bad list
    # only contains slugs that fail even after lowercasing.
    for bad in ("-foo", "foo-", "foo.bar", "a" * 50, "", "foo_bar"):
        r = _post_project(bad, "Bad")
        assert r.status_code == 400, f"expected 400 for slug={bad!r}, got {r.status_code}"
    print("  [OK] 6 bad slugs all rejected (uppercase is auto-normalized, not rejected)")


def test_duplicate_slug_conflict():
    print("--- T4: duplicate slug → 409 ---")
    s = _slug()
    r = _post_project(s, "First")
    assert r.status_code == 201
    r = _post_project(s, "Second")
    assert r.status_code == 409, r.status_code
    print(f"  [OK] {s}")


def test_archive_excludes_from_default_list():
    print("--- T5: archive hides from default list, shows with include_archived ---")
    s = _slug()
    assert _post_project(s, "Throwaway").status_code == 201
    r = httpx.post(f"{BASE}/api/dashboard/projects/{s}/archive", headers=ORIGIN, timeout=5)
    assert r.status_code == 200, r.text[:200]
    listed = httpx.get(f"{BASE}/api/dashboard/projects", headers=BEARER, timeout=5).json()["projects"]
    slugs = [p["slug"] for p in listed]
    assert s not in slugs, f"archived project still in default list: {slugs}"
    listed_all = httpx.get(f"{BASE}/api/dashboard/projects?include_archived=true",
                           headers=BEARER, timeout=5).json()["projects"]
    slugs_all = [p["slug"] for p in listed_all]
    assert s in slugs_all, f"include_archived missed archived project: {slugs_all}"
    print(f"  [OK]")


def test_archive_default_rejected():
    print("--- T6: cannot archive the 'default' project ---")
    r = httpx.post(f"{BASE}/api/dashboard/projects/default/archive", headers=ORIGIN, timeout=5)
    assert r.status_code == 400, r.status_code
    print("  [OK]")


async def test_two_projects_dont_collide_on_active_mission():
    print("--- T7: two projects each have their own active mission simultaneously ---")
    a, b = _slug(), _slug()
    assert _post_project(a, "Alpha").status_code == 201
    assert _post_project(b, "Beta").status_code == 201
    async with Client(f"{MCP}?project={a}", auth=KEY) as c:
        d = _unwrap(await c.call_tool("create_mission", {"name": "a-mission", "spec": "a"}))
        assert d["name"] == "a-mission"
    async with Client(f"{MCP}?project={b}", auth=KEY) as c:
        d = _unwrap(await c.call_tool("create_mission", {"name": "b-mission", "spec": "b"}))
        assert d["name"] == "b-mission"
    # Both still active (the per-project partial unique index allows this).
    async with Client(f"{MCP}?project={a}", auth=KEY) as c:
        d = _unwrap(await c.call_tool("get_active_mission", {}))
        assert d["name"] == "a-mission", f"alpha lost its mission: {d}"
    async with Client(f"{MCP}?project={b}", auth=KEY) as c:
        d = _unwrap(await c.call_tool("get_active_mission", {}))
        assert d["name"] == "b-mission", f"beta lost its mission: {d}"
    print(f"  [OK] alpha + beta hold their own actives")


async def test_inbox_isolation():
    print("--- T8: per-project inbox isolation ---")
    a, b = _slug(), _slug()
    assert _post_project(a, "A").status_code == 201
    assert _post_project(b, "B").status_code == 201
    # Post to A's inbox
    UNIQUE = f"isolate-marker-{_slug()}"
    r = httpx.post(f"{BASE}/api/dashboard/send-to-planner?project={a}",
                   json={"body": UNIQUE}, headers=ORIGIN, timeout=5)
    assert r.status_code == 200
    # A sees it
    inbox_a = httpx.get(f"{BASE}/api/dashboard/state?project={a}", headers=BEARER, timeout=5).json()["inbox"]
    assert any(m["body"] == UNIQUE for m in inbox_a), f"A missing own msg: {inbox_a}"
    # B doesn't see it
    inbox_b = httpx.get(f"{BASE}/api/dashboard/state?project={b}", headers=BEARER, timeout=5).json()["inbox"]
    assert not any(m["body"] == UNIQUE for m in inbox_b), f"B leaked A's msg: {inbox_b}"
    # Default doesn't see it
    inbox_d = httpx.get(f"{BASE}/api/dashboard/state", headers=BEARER, timeout=5).json()["inbox"]
    assert not any(m["body"] == UNIQUE for m in inbox_d), f"default leaked A's msg"
    print(f"  [OK] inbox stays put")


async def test_wait_for_user_message_scoped():
    print("--- T9: wait_for_user_message only fires on its project's posts ---")
    a, b = _slug(), _slug()
    assert _post_project(a, "WA").status_code == 201
    assert _post_project(b, "WB").status_code == 201
    UNIQUE = f"wait-marker-{_slug()}"

    # Schedule a post to B while A is waiting — A should NOT see it (would time out)
    async def writer_to_b():
        await asyncio.sleep(0.3)
        httpx.post(f"{BASE}/api/dashboard/send-to-planner?project={b}",
                   json={"body": UNIQUE}, headers=ORIGIN, timeout=5)

    async with Client(f"{MCP}?project={a}", auth=KEY) as ca:
        asyncio.create_task(writer_to_b())
        # Short timeout — if A picks up B's message, this assertion fails
        r = await ca.call_tool("wait_for_user_message", {"timeout_seconds": 3})
        d = _unwrap(r)
        assert d.get("status") == "pending", f"A picked up B's msg (leak!): {d}"

    # And B picks it up immediately when it asks
    async with Client(f"{MCP}?project={b}", auth=KEY) as cb:
        r = await cb.call_tool("wait_for_user_message", {"timeout_seconds": 2})
        d = _unwrap(r)
        assert d.get("body") == UNIQUE, f"B missed own msg: {d}"
        await cb.call_tool("ack_message", {"message_id": d["message_id"]})
    print("  [OK] no cross-project wait_for_user_message leak")


def test_legacy_callers_land_in_default():
    print("--- T10: legacy URL with NO ?project= still works, lands in 'default' ---")
    # Posting without ?project= should hit default
    UNIQUE = f"legacy-marker-{_slug()}"
    r = httpx.post(f"{BASE}/api/dashboard/send-to-planner",
                   json={"body": UNIQUE}, headers=ORIGIN, timeout=5)
    assert r.status_code == 200, r.text[:200]
    # And the message is in default's inbox
    inbox = httpx.get(f"{BASE}/api/dashboard/state", headers=BEARER, timeout=5).json()["inbox"]
    assert any(m["body"] == UNIQUE for m in inbox), f"default inbox missing: {[m['body'] for m in inbox]}"
    print("  [OK]")


def test_state_unknown_project_is_empty_not_error():
    print("--- T11: GET /state?project=does-not-exist returns empty (not 4xx) ---")
    r = httpx.get(f"{BASE}/api/dashboard/state?project=does-not-exist-{_slug()}",
                  headers=BEARER, timeout=5)
    assert r.status_code == 200, r.status_code
    s = r.json()
    assert s["active_mission"] is None
    assert s["inbox"] == []
    assert s["recent_missions"] == []
    print("  [OK]")


def test_send_to_unknown_project_returns_error():
    print("--- T12: send-to-planner with unknown project returns ok=False error ---")
    r = httpx.post(f"{BASE}/api/dashboard/send-to-planner?project=does-not-exist-{_slug()}",
                   json={"body": "should be rejected"}, headers=ORIGIN, timeout=5)
    # Per the dashboard handler shape: business-state error is 200 ok=false
    assert r.status_code == 200, r.status_code
    body = r.json()
    assert body.get("ok") is False, body
    assert "does not exist" in body.get("error", ""), body
    print("  [OK]")


async def test_oauth_endpoints_unaffected_by_project():
    print("--- T13: OAuth metadata is project-orthogonal ---")
    # AS metadata is reachable with or without ?project= — same response
    r1 = httpx.get(f"{BASE}/.well-known/oauth-authorization-server", timeout=5)
    r2 = httpx.get(f"{BASE}/.well-known/oauth-authorization-server?project=anything", timeout=5)
    assert r1.status_code == 200 and r2.status_code == 200
    assert r1.json() == r2.json(), "OAuth metadata varies by project — should be orthogonal"
    print("  [OK] OAuth surface unchanged")


async def main():
    test_create_project_happy_path()
    test_default_slug_reserved()
    test_invalid_slug_rejected()
    test_duplicate_slug_conflict()
    test_archive_excludes_from_default_list()
    test_archive_default_rejected()
    await test_two_projects_dont_collide_on_active_mission()
    await test_inbox_isolation()
    await test_wait_for_user_message_scoped()
    test_legacy_callers_land_in_default()
    test_state_unknown_project_is_empty_not_error()
    test_send_to_unknown_project_returns_error()
    await test_oauth_endpoints_unaffected_by_project()
    print("\nALL PROJECTS TESTS PASS")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except AssertionError as e:
        print(f"\nFAILED: {e}", file=sys.stderr)
        sys.exit(1)
    except asyncio.TimeoutError as e:
        print(f"\nFAILED (timeout): {e}", file=sys.stderr)
        sys.exit(1)
