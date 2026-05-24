"""v1.4 dashboard tests: login flow, cookie/bearer auth, state payload shape, integration.

Run against a fresh local server:
    AGENTSHIVE_API_KEY=test-key PORT=8001 TOOL_BLOCK_TIMEOUT_SECONDS=8 \
        python -m agentshive.main &
    python tests/test_dashboard.py
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


def _c(r):
    return r.structured_content if r.structured_content is not None else (r.content[0].text if r.content else None)


def test_login_bad_key_returns_200_with_error():
    print("--- T1: POST /dashboard/login bad key → 200 + error, no cookie ---")
    r = httpx.post(f"{BASE}/dashboard/login", data={"api_key": "wrong"}, follow_redirects=False)
    assert r.status_code == 200, f"expected 200, got {r.status_code}"
    assert "Invalid API key" in r.text, "error message missing"
    assert "agentshive_dash_session" not in r.headers.get("set-cookie", ""), "should not set cookie on bad key"
    print("  [OK]")


def test_login_good_key_redirects_with_cookie():
    print("--- T2: POST /dashboard/login good key → 302 + cookie ---")
    r = httpx.post(f"{BASE}/dashboard/login", data={"api_key": KEY}, follow_redirects=False)
    assert r.status_code == 302, f"expected 302, got {r.status_code}"
    assert r.headers.get("location") == "/dashboard"
    assert "agentshive_dash_session" in r.headers.get("set-cookie", "")
    cookie = r.cookies.get("agentshive_dash_session")
    assert cookie, "cookie value should be present"
    print(f"  [OK] cookie set ({len(cookie)} bytes)")
    return cookie


def test_dashboard_no_cookie_redirects():
    print("--- T3: GET /dashboard no cookie → 302 /dashboard/login ---")
    r = httpx.get(f"{BASE}/dashboard", follow_redirects=False)
    assert r.status_code == 302, f"expected 302, got {r.status_code}"
    assert r.headers.get("location") == "/dashboard/login"
    print("  [OK]")


def test_dashboard_valid_cookie_returns_html():
    print("--- T4: GET /dashboard valid cookie → 200 HTML ---")
    cookie = test_login_good_key_redirects_with_cookie.__wrapped__() if hasattr(test_login_good_key_redirects_with_cookie, "__wrapped__") else None
    if not cookie:
        login = httpx.post(f"{BASE}/dashboard/login", data={"api_key": KEY}, follow_redirects=False)
        cookie = login.cookies.get("agentshive_dash_session")
    r = httpx.get(f"{BASE}/dashboard", cookies={"agentshive_dash_session": cookie}, follow_redirects=False)
    assert r.status_code == 200, f"expected 200, got {r.status_code}"
    assert "AgentsHive Dashboard" in r.text
    print("  [OK] HTML rendered")


def test_dashboard_tampered_cookie_redirects():
    print("--- T5: GET /dashboard tampered cookie → 302 ---")
    r = httpx.get(
        f"{BASE}/dashboard",
        cookies={"agentshive_dash_session": "tampered.garbage.value"},
        follow_redirects=False,
    )
    assert r.status_code == 302, f"expected 302, got {r.status_code}"
    assert r.headers.get("location") == "/dashboard/login"
    print("  [OK] tampered cookie rejected")


def test_state_no_auth_401():
    print("--- T6: GET /api/dashboard/state no auth → 401 ---")
    r = httpx.get(f"{BASE}/api/dashboard/state")
    assert r.status_code == 401, f"expected 401, got {r.status_code}"
    print("  [OK]")


def test_state_bearer_200_shape():
    print("--- T7: GET /api/dashboard/state bearer → 200 with all keys ---")
    r = httpx.get(f"{BASE}/api/dashboard/state", headers={"Authorization": f"Bearer {KEY}"})
    assert r.status_code == 200, f"expected 200, got {r.status_code}"
    body = r.json()
    required = {"active_mission", "recent_missions", "pending_questions", "pending_summaries",
                "messages", "server_info", "coder_heartbeat"}
    missing = required - set(body.keys())
    assert not missing, f"missing keys: {missing}"
    assert {"coder_to_planner", "planner_to_coder"} <= set(body["messages"].keys())
    assert {"server_version", "tools_catalog_hash", "started_at"} <= set(body["server_info"].keys())
    assert {"last_seen", "freshness_seconds"} <= set(body["coder_heartbeat"].keys())
    print(f"  [OK] all top-level keys present; server v{body['server_info']['server_version']}")


def test_state_cookie_200_same_shape():
    print("--- T8: GET /api/dashboard/state cookie → 200 same shape ---")
    login = httpx.post(f"{BASE}/dashboard/login", data={"api_key": KEY}, follow_redirects=False)
    cookie = login.cookies.get("agentshive_dash_session")
    r = httpx.get(f"{BASE}/api/dashboard/state", cookies={"agentshive_dash_session": cookie})
    assert r.status_code == 200
    body = r.json()
    assert "active_mission" in body and "server_info" in body
    print("  [OK]")


def test_logout_clears_cookie():
    print("--- T9: POST /dashboard/logout → 302 + cookie cleared ---")
    login = httpx.post(f"{BASE}/dashboard/login", data={"api_key": KEY}, follow_redirects=False)
    cookie = login.cookies.get("agentshive_dash_session")
    r = httpx.post(f"{BASE}/dashboard/logout",
                   cookies={"agentshive_dash_session": cookie},
                   follow_redirects=False)
    assert r.status_code == 302
    set_cookie = r.headers.get("set-cookie", "")
    assert "agentshive_dash_session" in set_cookie, "logout should set cookie header to clear it"
    # Deleted cookies have Max-Age=0 or expires in the past
    assert "max-age=0" in set_cookie.lower() or "expires=" in set_cookie.lower(), set_cookie
    print("  [OK]")


def test_logout_works_without_valid_cookie():
    print("--- T10: POST /dashboard/logout no cookie → 302 (public) ---")
    r = httpx.post(f"{BASE}/dashboard/logout", follow_redirects=False)
    assert r.status_code == 302, f"logout should work without a cookie, got {r.status_code}"
    print("  [OK]")


async def test_integration_state_populated_after_writes():
    print("--- T11: integration — writes populate the state payload ---")
    async with Client(MCP_URL, auth=KEY) as cli:
        await cli.call_tool("create_mission", {"name": "dash-int", "spec": "integration test"})
        await cli.call_tool("send_to_coder", {"body": "fyi for the coder"})
        async def ask():
            async with Client(MCP_URL, auth=KEY) as c2:
                await c2.call_tool("ask_planner", {"question": "what should I do?"})
        async def submit():
            async with Client(MCP_URL, auth=KEY) as c2:
                await c2.call_tool("submit_progress", {"summary": "progress made"})
        asyncio.create_task(ask())
        asyncio.create_task(submit())
        await asyncio.sleep(1.0)  # let inserts settle

    r = httpx.get(f"{BASE}/api/dashboard/state", headers={"Authorization": f"Bearer {KEY}"})
    body = r.json()
    assert body["active_mission"] and body["active_mission"]["name"] == "dash-int"
    assert "spec_preview" in body["active_mission"]
    assert len(body["pending_questions"]) >= 1, f"expected pending question, got {body['pending_questions']}"
    assert len(body["pending_summaries"]) >= 1
    assert len(body["messages"]["planner_to_coder"]) >= 1
    print(f"  [OK] active={body['active_mission']['name']} "
          f"q={len(body['pending_questions'])} s={len(body['pending_summaries'])} "
          f"p2c={len(body['messages']['planner_to_coder'])}")


def main():
    test_login_bad_key_returns_200_with_error()
    test_login_good_key_redirects_with_cookie()
    test_dashboard_no_cookie_redirects()
    test_dashboard_valid_cookie_returns_html()
    test_dashboard_tampered_cookie_redirects()
    test_state_no_auth_401()
    test_state_bearer_200_shape()
    test_state_cookie_200_same_shape()
    test_logout_clears_cookie()
    test_logout_works_without_valid_cookie()
    asyncio.run(test_integration_state_populated_after_writes())
    print("\nALL DASHBOARD TESTS PASS")


if __name__ == "__main__":
    try:
        main()
    except AssertionError as e:
        print(f"\nFAILED: {e}", file=sys.stderr)
        sys.exit(1)
