"""v1.7 OAuth 2.1 surface tests.

Covers the SDK-mounted /authorize, /token, /register, /revoke,
the two /.well-known/* metadata routes, our custom /oauth/consent page,
and the RFC 8707 audience guard. Also re-verifies that the legacy
shared-bearer-key path keeps working (Q2 KEEP LEGACY KEY FOREVER).

Run against a fresh local server:
    AGENTSHIVE_API_KEY=test-key PORT=8000 AGENTSHIVE_BASE_URL=http://localhost:8000 \
        TOOL_BLOCK_TIMEOUT_SECONDS=20 python -m agentshive.main &
    AGENTSHIVE_BASE=http://localhost:8000 python tests/test_oauth.py
"""

import base64
import hashlib
import os
import sys
from urllib.parse import parse_qs, urlparse

import httpx

KEY = os.environ.get("AGENTSHIVE_API_KEY", "test-key")
BASE = os.environ.get("AGENTSHIVE_BASE", "http://localhost:8000")
RESOURCE = f"{BASE}/mcp"

VERIFIER = "a" * 64
CHALLENGE = base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest()).decode().rstrip("=")


def _register_client(c: httpx.Client) -> str:
    r = c.post(f"{BASE}/register", json={
        "redirect_uris": ["http://localhost:54545/cb"],
        "client_name": "test client",
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    })
    assert r.status_code == 201, f"register failed: {r.status_code} {r.text[:200]}"
    return r.json()["client_id"]


def _consent_approve(c: httpx.Client, cid: str, *, state: str = "s", api_key: str = KEY) -> str:
    r = c.post(f"{BASE}/oauth/consent", data={
        "client_id": cid,
        "redirect_uri": "http://localhost:54545/cb",
        "code_challenge": CHALLENGE,
        "state": state,
        "scopes": "mcp",
        "resource": RESOURCE,
        "redirect_uri_provided_explicitly": "1",
        "api_key": api_key,
        "decision": "approve",
    }, follow_redirects=False)
    assert r.status_code == 302, f"consent approve expected 302, got {r.status_code}: {r.text[:200]}"
    loc = r.headers["location"]
    q = parse_qs(urlparse(loc).query)
    assert "code" in q and q["state"][0] == state, f"bad redirect: {loc}"
    return q["code"][0]


def test_as_metadata():
    print("--- T1: GET /.well-known/oauth-authorization-server ---")
    r = httpx.get(f"{BASE}/.well-known/oauth-authorization-server", timeout=5)
    assert r.status_code == 200, r.status_code
    md = r.json()
    for required in ("issuer", "authorization_endpoint", "token_endpoint",
                     "registration_endpoint", "revocation_endpoint",
                     "code_challenge_methods_supported"):
        assert required in md, f"missing {required}"
    assert "S256" in md["code_challenge_methods_supported"]
    print("  [OK] AS metadata complete")


def test_prm_metadata():
    print("--- T2: GET /.well-known/oauth-protected-resource/mcp ---")
    r = httpx.get(f"{BASE}/.well-known/oauth-protected-resource/mcp", timeout=5)
    assert r.status_code == 200, r.status_code
    md = r.json()
    assert md["resource"] == RESOURCE, f"resource mismatch: {md.get('resource')!r} vs {RESOURCE!r}"
    print(f"  [OK] PRM resource={md['resource']}")


def test_dcr_registers_client():
    print("--- T3: POST /register issues a client_id ---")
    with httpx.Client(timeout=5) as c:
        cid = _register_client(c)
    assert cid and isinstance(cid, str)
    print(f"  [OK] DCR issued client_id={cid[:14]}...")


def test_authorize_redirects_to_consent():
    print("--- T4: GET /authorize 302's to /oauth/consent ---")
    with httpx.Client(timeout=5) as c:
        cid = _register_client(c)
        r = c.get(f"{BASE}/authorize", params={
            "response_type": "code", "client_id": cid,
            "redirect_uri": "http://localhost:54545/cb",
            "code_challenge": CHALLENGE, "code_challenge_method": "S256",
            "state": "s", "scope": "mcp", "resource": RESOURCE,
        }, follow_redirects=False)
        assert r.status_code in (302, 307), f"expected redirect, got {r.status_code}"
        loc = r.headers["location"]
        assert "/oauth/consent" in loc, f"wrong redirect target: {loc}"
        print(f"  [OK] /authorize -> /oauth/consent")


def test_consent_get_shows_api_key_field_without_cookie():
    print("--- T5: GET /oauth/consent without cookie shows api_key input ---")
    with httpx.Client(timeout=5) as c:
        cid = _register_client(c)
        r = c.get(f"{BASE}/oauth/consent", params={
            "client_id": cid,
            "redirect_uri": "http://localhost:54545/cb",
            "code_challenge": CHALLENGE, "state": "s",
            "scopes": "mcp", "resource": RESOURCE,
            "redirect_uri_provided_explicitly": "1",
        })
        assert r.status_code == 200
        assert 'name="api_key"' in r.text, "api_key input missing"
        print("  [OK] api_key field present")


def test_consent_get_unknown_client_rejected():
    print("--- T6: GET /oauth/consent for unknown client_id rejected ---")
    r = httpx.get(f"{BASE}/oauth/consent", params={
        "client_id": "does-not-exist",
        "redirect_uri": "http://localhost:54545/cb",
        "code_challenge": CHALLENGE, "state": "s",
        "scopes": "mcp", "resource": RESOURCE,
        "redirect_uri_provided_explicitly": "1",
    }, timeout=5)
    assert r.status_code == 400, r.status_code
    print("  [OK] unknown client → 400")


def test_consent_deny_redirects_with_error():
    print("--- T7: POST /oauth/consent deny → 302 with error=access_denied ---")
    with httpx.Client(timeout=5) as c:
        cid = _register_client(c)
        r = c.post(f"{BASE}/oauth/consent", data={
            "client_id": cid, "redirect_uri": "http://localhost:54545/cb",
            "code_challenge": CHALLENGE, "state": "denied", "scopes": "mcp",
            "resource": RESOURCE, "redirect_uri_provided_explicitly": "1",
            "decision": "deny",
        }, follow_redirects=False)
        assert r.status_code == 302
        assert "error=access_denied" in r.headers["location"]
        assert "state=denied" in r.headers["location"]
        print("  [OK] deny redirect carries error + state")


def test_consent_bad_api_key_rerenders_form():
    print("--- T8: POST /oauth/consent with wrong api_key re-renders with error ---")
    with httpx.Client(timeout=5) as c:
        cid = _register_client(c)
        r = c.post(f"{BASE}/oauth/consent", data={
            "client_id": cid, "redirect_uri": "http://localhost:54545/cb",
            "code_challenge": CHALLENGE, "state": "s", "scopes": "mcp",
            "resource": RESOURCE, "redirect_uri_provided_explicitly": "1",
            "api_key": "WRONG", "decision": "approve",
        }, follow_redirects=False)
        assert r.status_code == 200, r.status_code
        assert "Invalid API key" in r.text
        print("  [OK] bad-key form re-render")


def test_full_pkce_token_flow_and_rotation():
    print("--- T9: PKCE happy path + refresh rotation invalidates the old token ---")
    with httpx.Client(timeout=5) as c:
        cid = _register_client(c)
        code = _consent_approve(c, cid)
        r = c.post(f"{BASE}/token", data={
            "grant_type": "authorization_code", "code": code,
            "redirect_uri": "http://localhost:54545/cb", "client_id": cid,
            "code_verifier": VERIFIER, "resource": RESOURCE,
        })
        assert r.status_code == 200, f"token mint failed: {r.status_code} {r.text[:200]}"
        tok = r.json()
        assert tok["expires_in"] == 3600, tok["expires_in"]
        # Refresh once and assert both tokens rotate
        r2 = c.post(f"{BASE}/token", data={
            "grant_type": "refresh_token", "refresh_token": tok["refresh_token"],
            "client_id": cid,
        })
        assert r2.status_code == 200, r2.text[:200]
        tok2 = r2.json()
        assert tok2["access_token"] != tok["access_token"], "access must rotate"
        assert tok2["refresh_token"] != tok["refresh_token"], "refresh MUST rotate (RFC 9700 BCP)"
        # Replaying the old refresh must be rejected
        r3 = c.post(f"{BASE}/token", data={
            "grant_type": "refresh_token", "refresh_token": tok["refresh_token"],
            "client_id": cid,
        })
        assert r3.json().get("error") == "invalid_grant", f"old refresh accepted: {r3.text[:200]}"
        print("  [OK] full PKCE + rotation + replay-rejection")


def test_authorize_rejects_wrong_audience():
    print("--- T10: /authorize with mismatched resource is refused ---")
    with httpx.Client(timeout=5) as c:
        cid = _register_client(c)
        r = c.get(f"{BASE}/authorize", params={
            "response_type": "code", "client_id": cid,
            "redirect_uri": "http://localhost:54545/cb",
            "code_challenge": CHALLENGE, "code_challenge_method": "S256",
            "state": "s", "scope": "mcp",
            "resource": "http://attacker.example/api",
        }, follow_redirects=False)
        # SDK converts our ValueError into an OAuth error redirect; either a 302
        # to the registered redirect_uri with error= OR a 4xx server response.
        assert r.status_code in (302, 307, 400, 500), r.status_code
        if r.status_code in (302, 307):
            assert "error=" in r.headers.get("location", "")
        print(f"  [OK] wrong audience rejected (status {r.status_code})")


def test_revoke_invalidates_access_token():
    print("--- T11: POST /revoke renders the access token unusable ---")
    with httpx.Client(timeout=5) as c:
        cid = _register_client(c)
        code = _consent_approve(c, cid, state="rv")
        r = c.post(f"{BASE}/token", data={
            "grant_type": "authorization_code", "code": code,
            "redirect_uri": "http://localhost:54545/cb", "client_id": cid,
            "code_verifier": VERIFIER, "resource": RESOURCE,
        })
        tok = r.json()
        # Sanity: token works for /mcp initialize
        probe = c.post(f"{BASE}/mcp", headers={
            "Authorization": f"Bearer {tok['access_token']}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }, json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2024-11-05", "capabilities": {},
            "clientInfo": {"name": "probe", "version": "1"},
        }})
        assert probe.status_code == 200, f"OAuth token must work on /mcp: {probe.status_code}"
        # Revoke
        rev = c.post(f"{BASE}/revoke", data={
            "token": tok["access_token"], "client_id": cid, "client_secret": "",
        })
        assert rev.status_code == 200, rev.status_code
        # Probe again — must be 401 now
        probe2 = c.post(f"{BASE}/mcp", headers={
            "Authorization": f"Bearer {tok['access_token']}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }, json={"jsonrpc": "2.0", "id": 2, "method": "initialize", "params": {
            "protocolVersion": "2024-11-05", "capabilities": {},
            "clientInfo": {"name": "probe", "version": "1"},
        }})
        assert probe2.status_code == 401, f"revoked token still works: {probe2.status_code}"
        print("  [OK] /revoke takes effect on subsequent /mcp request")


def test_legacy_bearer_key_still_works():
    print("--- T12: Q2 — legacy AGENTSHIVE_API_KEY bearer still authenticates /mcp ---")
    r = httpx.post(f"{BASE}/mcp", headers={
        "Authorization": f"Bearer {KEY}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }, json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2024-11-05", "capabilities": {},
        "clientInfo": {"name": "probe", "version": "1"},
    }}, timeout=5)
    assert r.status_code == 200, f"legacy key path broken: {r.status_code} {r.text[:200]}"
    print("  [OK] legacy bearer still accepted")


def test_wrong_bearer_key_rejected():
    print("--- T13: a random bearer token is 401'd ---")
    r = httpx.post(f"{BASE}/mcp", headers={
        "Authorization": "Bearer not-a-real-token-xxx",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }, json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2024-11-05", "capabilities": {},
        "clientInfo": {"name": "probe", "version": "1"},
    }}, timeout=5)
    assert r.status_code == 401, r.status_code
    print("  [OK] unknown bearer → 401")


def main():
    test_as_metadata()
    test_prm_metadata()
    test_dcr_registers_client()
    test_authorize_redirects_to_consent()
    test_consent_get_shows_api_key_field_without_cookie()
    test_consent_get_unknown_client_rejected()
    test_consent_deny_redirects_with_error()
    test_consent_bad_api_key_rerenders_form()
    test_full_pkce_token_flow_and_rotation()
    test_authorize_rejects_wrong_audience()
    test_revoke_invalidates_access_token()
    test_legacy_bearer_key_still_works()
    test_wrong_bearer_key_rejected()
    print("\nALL OAUTH TESTS PASS")


if __name__ == "__main__":
    try:
        main()
    except AssertionError as e:
        print(f"\nFAILED: {e}", file=sys.stderr)
        sys.exit(1)
