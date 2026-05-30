"""v2.x long-lived agent tokens (in-process).

Tenant resolution, revocation, cross-tenant isolation, label validation,
rate-limiting. The HTTP middleware integration is verified live against a real
deploy; here we exercise the security-critical logic in-process: mint -> store
hash -> tenant_for_agent_token resolves to the operator's sub (NOT legacy), revoke
flips the lookup to None, and a different tenant can't see or revoke a foreign
token.

Run:
    PYTHONPATH=src python tests/test_agent_tokens.py
"""

import os
import sys

DB = os.environ.get("AGENTSHIVE_TEST_DB", "sqlite:///./agentshive_v2_agent_tokens_test.db")
# In-process test -- no HTTP client involved (the `localhost`-in-URL guard from
# [feedback-tests-must-assert-localhost] applies to tests that DO open clients).
# This sqlite-only guard is the moral equivalent: prevents pointing this run at
# a real Postgres URL by accident.
assert DB.startswith("sqlite"), f"refusing non-sqlite test DB: {DB}"

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if os.path.isdir(_SRC) and _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from contextlib import contextmanager  # noqa: E402

from sqlmodel import Session, select  # noqa: E402

from agentshive.config import Settings  # noqa: E402
from agentshive.db import (  # noqa: E402
    AgentToken, get_engine, get_or_create_tenant, init_engine, tenant_for_agent_token,
)
from agentshive.project import PROJECT_CONTEXT  # noqa: E402
from agentshive.tenant import (  # noqa: E402
    IDENTITY_CONTEXT, LEGACY_TENANT, TENANT_CONTEXT,
)
from agentshive.web import (  # noqa: E402
    do_create_agent_token, do_list_agent_tokens, do_revoke_agent_token,
)


@contextmanager
def ctx(tenant=None, identity=None, project="default"):
    t1 = TENANT_CONTEXT.set(tenant) if tenant is not None else None
    t2 = IDENTITY_CONTEXT.set(identity)
    t3 = PROJECT_CONTEXT.set(project)
    try:
        yield
    finally:
        if t1 is not None:
            TENANT_CONTEXT.reset(t1)
        IDENTITY_CONTEXT.reset(t2)
        PROJECT_CONTEXT.reset(t3)


def setup():
    path = DB.replace("sqlite:///", "").replace("sqlite://", "")
    if path and path != ":memory:" and os.path.exists(path):
        os.remove(path)
    init_engine(Settings(
        api_key="k", database_url=DB, port=8000,
        poll_interval_seconds=0.05, tool_block_timeout_seconds=1,
        supabase_url=None,
    ))


def test_mint_then_resolve():
    """Operator mints a token -> using it as bearer resolves to their sub (NOT legacy)."""
    print("--- T1: mint -> resolve to operator's sub ---")
    sub = "11111111-2222-3333-4444-aaaaaaaaaaaa"
    with ctx(tenant=sub, identity={"sub": sub}):
        res, status = do_create_agent_token("desktop-test")
        assert status == 200, res
        assert res["token"].startswith("ahat_"), res["token"]
        assert len(res["token"]) == 45, len(res["token"])  # ahat_ (5) + 40 base64
        assert res["prefix"] == res["token"][5:13], res
        full_token = res["token"]
    # Resolution returns the operator's sub -- NOT LEGACY_TENANT.
    assert tenant_for_agent_token(full_token) == sub
    assert tenant_for_agent_token(full_token) != LEGACY_TENANT
    print("  [OK] minted ahat_ token -> tenant=sub")


def test_revoked_token_is_rejected():
    """A revoked token resolves to None (-> UNAUTHENTICATED_TENANT at the middleware)."""
    print("--- T2: revoke -> tenant_for_agent_token returns None ---")
    sub = "22222222-3333-4444-5555-bbbbbbbbbbbb"
    with ctx(tenant=sub, identity={"sub": sub}):
        res, _ = do_create_agent_token("revoke-me")
        full_token = res["token"]
        token_id = res["id"]
        # It works first.
        assert tenant_for_agent_token(full_token) == sub
        # Revoke it.
        rev, status = do_revoke_agent_token(token_id)
        assert status == 200 and rev["ok"] is True, (status, rev)
    # Now use it -- rejected.
    assert tenant_for_agent_token(full_token) is None
    # Idempotent revoke: a second DELETE on the already-revoked id stays 200.
    with ctx(tenant=sub, identity={"sub": sub}):
        rev2, status2 = do_revoke_agent_token(token_id)
        assert status2 == 200 and rev2["ok"] is True, (status2, rev2)
    print("  [OK] revoked -> None; revoke is idempotent")


def test_cross_tenant_isolation():
    """Tenant A mints; tenant B can't see in GET, can't DELETE (404, never leak)."""
    print("--- T3: cross-tenant: GET scoped, DELETE 404 ---")
    sub_a = "aaaaaaaa-3333-4444-5555-cccccccccccc"
    sub_b = "bbbbbbbb-3333-4444-5555-dddddddddddd"
    with ctx(tenant=sub_a, identity={"sub": sub_a}):
        res_a, _ = do_create_agent_token("a-machine")
        token_a_id = res_a["id"]
        token_a_full = res_a["token"]
    with ctx(tenant=sub_b, identity={"sub": sub_b}):
        listing = do_list_agent_tokens()
        assert all(t["id"] != token_a_id for t in listing["tokens"]), listing
        # Tenant B can't revoke A's token -- 404, no existence leak.
        rev, status = do_revoke_agent_token(token_a_id)
        assert status == 404, (status, rev)
    # A's token is still active + still resolves to A.
    assert tenant_for_agent_token(token_a_full) == sub_a
    with Session(get_engine()) as s:
        row = s.exec(select(AgentToken).where(AgentToken.id == token_a_id)).first()
        assert row is not None and row.revoked_at is None
    print("  [OK] tenant B cannot see / revoke / use tenant A's token")


def test_malformed_ahat_rejected():
    """Malformed `ahat_` tokens resolve to None (not 500)."""
    print("--- T4: malformed ahat_ -> None ---")
    for bad in ["ahat_", "ahat_invalid", "ahat_" + "x" * 40, "not-an-ahat", "", None]:
        try:
            res = tenant_for_agent_token(bad or "")
        except Exception as e:  # noqa: BLE001
            raise AssertionError(f"tenant_for_agent_token raised on {bad!r}: {e}") from e
        assert res is None, (bad, res)
    print("  [OK] malformed tokens reject cleanly")


def test_non_ahat_falls_through():
    """tenant_for_agent_token only handles ahat_ -- non-ahat tokens return None so
    the middleware's legacy / Supabase JWT / OAuth branches still handle them."""
    print("--- T5: non-ahat tokens fall through (legacy/JWT/OAuth handle them) ---")
    for non_ahat in ["some-shared-key", "eyJhbGciOi...", "Bearer-style-but-not-ahat"]:
        assert tenant_for_agent_token(non_ahat) is None, non_ahat
    print("  [OK] non-ahat tokens are not claimed by the agent-token path")


def test_label_validation():
    """Reject empty / oversized / shell-meta labels."""
    print("--- T6: label validation ---")
    sub = "cccccccc-3333-4444-5555-eeeeeeeeeeee"
    with ctx(tenant=sub, identity={"sub": sub}):
        bad_labels = ["", "  ", "x" * 100, "evil; rm -rf /", "with$pipe|", "back`tick", "new\nline"]
        for bad in bad_labels:
            _, status = do_create_agent_token(bad)
            assert status == 400, f"expected 400 for {bad!r}, got {status}"
        # Good labels accepted.
        for good in ["desktop-MIKES-PC", "Office Laptop", "build.server@home", "x", "x" * 80]:
            _, status = do_create_agent_token(good)
            assert status == 200, f"expected 200 for {good!r}, got {status}"
    print("  [OK] bad labels rejected at 400; good labels accepted")


def test_listing_never_returns_secret():
    """GET never exposes the secret hash or plaintext."""
    print("--- T7: list response never includes the secret ---")
    sub = "eeeeeeee-3333-4444-5555-ffffffffffff"
    with ctx(tenant=sub, identity={"sub": sub}):
        res, _ = do_create_agent_token("private")
        listing = do_list_agent_tokens()
        assert listing["tokens"], listing
        for t in listing["tokens"]:
            assert "token" not in t, t
            assert "secret_hash" not in t, t
            assert "prefix" in t and "id" in t
    print("  [OK] list response carries id+prefix only -- no secret")


def test_rate_limit():
    """10 tokens / hour / tenant is enforced."""
    print("--- T8: rate-limit 10/h/tenant ---")
    sub = "ddddffff-3333-4444-5555-aabbccddeeff"
    with ctx(tenant=sub, identity={"sub": sub}):
        for i in range(10):
            _, status = do_create_agent_token(f"machine-{i}")
            assert status == 200, (i, status)
        _, status11 = do_create_agent_token("eleventh")
        assert status11 == 429, status11
    print("  [OK] 11th mint within an hour returns 429")


def main():
    setup()
    test_mint_then_resolve()
    test_revoked_token_is_rejected()
    test_cross_tenant_isolation()
    test_malformed_ahat_rejected()
    test_non_ahat_falls_through()
    test_label_validation()
    test_listing_never_returns_secret()
    test_rate_limit()
    print("ALL OK")


if __name__ == "__main__":
    main()
