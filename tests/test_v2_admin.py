"""v2.x admin + plan-gate (in-process).

The HTTP admin authz (admin token -> 200, legacy/none -> 403) is verified live
against real Supabase JWTs separately. Here we exercise the security-critical
LOGIC in-process: is_admin(), the trial/plan gate (pro_unlimited bypass, free
blocks after the 2nd report, LEGACY always exempt, admin/identity-plan fast
paths), and the banned flag.

Run:
    PYTHONPATH=src python tests/test_v2_admin.py
"""

import os
import sys

DB = os.environ.get("AGENTSHIVE_TEST_DB", "sqlite:///./agentshive_v2_admin_test.db")
assert DB.startswith("sqlite"), f"refusing non-sqlite test DB: {DB}"

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if os.path.isdir(_SRC) and _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from contextlib import contextmanager  # noqa: E402

from sqlmodel import Session  # noqa: E402

from agentshive import tools  # noqa: E402
from agentshive.config import Settings  # noqa: E402
from agentshive.db import (  # noqa: E402
    PLAN_FREE, PLAN_PRO_UNLIMITED, Tenant, get_engine, get_or_create_tenant,
    init_engine, is_tenant_banned,
)
from agentshive.project import PROJECT_CONTEXT  # noqa: E402
from agentshive.tenant import (  # noqa: E402
    IDENTITY_CONTEXT, LEGACY_TENANT, TENANT_CONTEXT, is_admin,
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
    init_engine(Settings(api_key="k", database_url=DB, port=8000,
                         poll_interval_seconds=0.05, tool_block_timeout_seconds=1,
                         supabase_url=None))


def test_is_admin():
    print("--- T1: is_admin() ---")
    os.environ.pop("ADMIN_EMAIL", None)
    with ctx(identity=None):
        assert is_admin() is False
    with ctx(identity={"sub": "x", "role": "user"}):
        assert is_admin() is False
    with ctx(identity={"sub": "x", "role": "admin"}):
        assert is_admin() is True
    os.environ["ADMIN_EMAIL"] = "boss@example.com"
    with ctx(identity={"sub": "x", "email": "boss@example.com"}):
        assert is_admin() is True
    with ctx(identity={"sub": "x", "email": "someoneelse@example.com"}):
        assert is_admin() is False
    os.environ.pop("ADMIN_EMAIL", None)
    print("  [OK] role + ADMIN_EMAIL paths; non-admins rejected")


def test_legacy_exempt():
    print("--- T2: legacy tenant is never gated ---")
    with ctx(tenant=LEGACY_TENANT):
        for _ in range(5):
            assert tools._check_plan_gate(count_report=True) is None
    print("  [OK] legacy exempt across many reports")


def test_free_trial_blocks_after_two():
    print("--- T3: free plan blocks after the 2nd report ---")
    sub = "11111111-2222-3333-4444-aaaaaaaaaaaa"
    with Session(get_engine()) as s:
        get_or_create_tenant(s, sub)  # defaults to free
    with ctx(tenant=sub, identity={"sub": sub}):
        assert tools._check_plan_gate(count_report=True) is None   # report 1 (->1)
        assert tools._check_plan_gate(count_report=True) is None   # report 2 (->2)
        blocked = tools._check_plan_gate(count_report=True)        # now >=2
        assert blocked and blocked.get("trial_ended") is True, blocked
        # non-report mutators are blocked too once the trial is over
        assert tools._check_plan_gate() is not None
    print("  [OK] free: 2 reports allowed, then trial_ended")


def test_pro_unlimited_bypasses():
    print("--- T4: pro_unlimited bypasses the gate ---")
    sub = "22222222-3333-4444-5555-bbbbbbbbbbbb"
    with Session(get_engine()) as s:
        t = get_or_create_tenant(s, sub)
        t.plan = PLAN_PRO_UNLIMITED
        t.trial_reports_used = 99  # even way over the limit
        s.add(t); s.commit()
    with ctx(tenant=sub, identity={"sub": sub}):
        for _ in range(5):
            assert tools._check_plan_gate(count_report=True) is None
    print("  [OK] pro_unlimited never gated (DB plan)")


def test_admin_fastpath_and_db_authoritative():
    print("--- T5: admin bypasses; DB plan is authoritative (JWT plan claim does NOT bypass) ---")
    sub = "33333333-4444-5555-6666-cccccccccccc"
    with Session(get_engine()) as s:
        t = get_or_create_tenant(s, sub)  # free, trial maxed
        t.trial_reports_used = 99; s.add(t); s.commit()
    # A bare JWT plan claim must NOT bypass — DB Tenant.plan (free+maxed) is the
    # single source of truth, so this is blocked.
    with ctx(tenant=sub, identity={"sub": sub, "plan": PLAN_PRO_UNLIMITED}):
        assert tools._check_plan_gate(count_report=True) is not None
    # admin role bypasses regardless of DB plan (role rides in the verified JWT).
    with ctx(tenant=sub, identity={"sub": sub, "role": "admin"}):
        assert tools._check_plan_gate(count_report=True) is None
    print("  [OK] admin bypasses; DB authoritative (no JWT-plan bypass)")


def test_banned_flag():
    print("--- T6: banned flag ---")
    sub = "44444444-5555-6666-7777-dddddddddddd"
    with Session(get_engine()) as s:
        t = get_or_create_tenant(s, sub)
        assert is_tenant_banned(sub) is False
        t.banned = True; s.add(t); s.commit()
    assert is_tenant_banned(sub) is True
    assert is_tenant_banned(LEGACY_TENANT) is False
    print("  [OK] is_tenant_banned reflects DB; legacy never banned")


def test_self_and_admin_protection():
    print("--- T7: ban/remove refuse self + other admins ---")
    from agentshive.admin import _self_or_admin_block
    os.environ.pop("ADMIN_EMAIL", None)
    with ctx(identity={"sub": "admin-sub", "role": "admin"}):
        assert _self_or_admin_block("admin-sub", None) is not None, "must refuse self"
        assert _self_or_admin_block("other", {"app_metadata": {"role": "admin"}}) is not None, "must refuse other admin"
        assert _self_or_admin_block("other", {"app_metadata": {"role": "user"}}) is None, "normal user allowed"
        os.environ["ADMIN_EMAIL"] = "boss@example.com"
        assert _self_or_admin_block("other", {"email": "boss@example.com", "app_metadata": {}}) is not None, "must refuse ADMIN_EMAIL account"
        os.environ.pop("ADMIN_EMAIL", None)
    print("  [OK] self + admin-target refused; normal user allowed")


def main():
    setup()
    test_is_admin()
    test_legacy_exempt()
    test_free_trial_blocks_after_two()
    test_pro_unlimited_bypasses()
    test_admin_fastpath_and_db_authoritative()
    test_banned_flag()
    test_self_and_admin_protection()
    print("\nALL v2 ADMIN/GATE TESTS PASS")


if __name__ == "__main__":
    try:
        main()
    except AssertionError as e:
        print(f"\nFAILED: {e}", file=sys.stderr)
        sys.exit(1)
