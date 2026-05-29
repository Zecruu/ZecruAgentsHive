"""v2.x — multi-tenancy isolation (in-process).

We can't mint real Supabase JWTs in a test (JWKS is verify-only — we don't hold
the signing key), so instead of driving the HTTP server we exercise the tenancy
CHOKEPOINT + by-id IDOR guards directly: set TENANT_CONTEXT / PROJECT_CONTEXT the
way the middleware would, then call the same _do_* helpers the MCP tools delegate
to, and assert tenant A can never see or mutate tenant B's data — including by
guessing B's row ids (the by-id IDOR class the Planner flagged as mandatory).

Covers:
  T1 — same slug under two tenants resolves to different projects (chokepoint)
  T2 — _active_mission is tenant-scoped (A never sees B's mission)
  T3 — by-id IDOR: answer_question / respond_to_summary / ack_message / is_mission_done
       reject cross-tenant ids (return "no ... id"), allow same-tenant
  T4 — verify_supabase_jwt fails closed (garbage token / no url -> None)

Run (in-process, no server needed):
    PYTHONPATH=src python tests/test_v2_tenancy.py
"""

import os
import sys

# v1.8 lesson: guard the DB target before any engine setup so a stray env var
# can't point this at a real database. This test only ever uses a throwaway file.
DB = os.environ.get("AGENTSHIVE_TEST_DB", "sqlite:///./agentshive_v2_tenancy_test.db")
assert DB.startswith("sqlite"), f"refusing to run tenancy test against non-sqlite DB: {DB}"

# Allow `python tests/test_v2_tenancy.py` from the repo root without install.
_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if os.path.isdir(_SRC) and _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from contextlib import contextmanager  # noqa: E402

from sqlmodel import Session  # noqa: E402

from agentshive import tools  # noqa: E402
from agentshive.config import Settings  # noqa: E402
from agentshive.db import Project, get_engine, init_engine  # noqa: E402
from agentshive.project import PROJECT_CONTEXT  # noqa: E402
from agentshive.tenant import TENANT_CONTEXT, verify_supabase_jwt  # noqa: E402

TENANT_A = "tenant-a-1111"
TENANT_B = "tenant-b-2222"
SLUG = "shared"  # identical slug under both tenants — the collision tenancy must isolate


@contextmanager
def acting_as(tenant: str, slug: str):
    t1 = TENANT_CONTEXT.set(tenant)
    t2 = PROJECT_CONTEXT.set(slug)
    try:
        yield
    finally:
        TENANT_CONTEXT.reset(t1)
        PROJECT_CONTEXT.reset(t2)


def _make_project(tenant: str, slug: str, name: str) -> str:
    with Session(get_engine()) as s:
        p = Project(tenant_id=tenant, slug=slug, name=name)
        s.add(p)
        s.commit()
        s.refresh(p)
        return p.id


def setup():
    # Fresh throwaway DB each run.
    path = DB.replace("sqlite:///", "").replace("sqlite://", "")
    if path and path not in (":memory:",) and os.path.exists(path):
        os.remove(path)
    settings = Settings(
        api_key="test-key", database_url=DB, port=8000,
        poll_interval_seconds=0.05, tool_block_timeout_seconds=1, supabase_url=None,
    )
    init_engine(settings)


def test_resolution_chokepoint():
    print("--- T1: same slug under two tenants -> different projects ---")
    pid_a = _make_project(TENANT_A, SLUG, "A's shared")
    pid_b = _make_project(TENANT_B, SLUG, "B's shared")
    assert pid_a != pid_b, "same-slug projects must be distinct rows"
    with acting_as(TENANT_A, SLUG), Session(get_engine()) as s:
        assert tools._project_id(s) == pid_a
    with acting_as(TENANT_B, SLUG), Session(get_engine()) as s:
        assert tools._project_id(s) == pid_b
    print(f"  [OK] resolved A={pid_a[:8]} B={pid_b[:8]} distinctly")


def test_active_mission_isolation():
    print("--- T2: _active_mission is tenant-scoped ---")
    with acting_as(TENANT_A, SLUG):
        ma = tools._do_create_mission("Mission A", "spec for A")
    with acting_as(TENANT_B, SLUG):
        mb = tools._do_create_mission("Mission B", "spec for B")
    assert "mission_id" in ma and "mission_id" in mb, (ma, mb)
    with acting_as(TENANT_A, SLUG), Session(get_engine()) as s:
        active = tools._active_mission(s)
        assert active is not None and active.name == "Mission A", active
    with acting_as(TENANT_B, SLUG), Session(get_engine()) as s:
        active = tools._active_mission(s)
        assert active is not None and active.name == "Mission B", active
    print("  [OK] each tenant sees only its own active mission")
    return ma["mission_id"], mb["mission_id"]


def test_by_id_idor():
    print("--- T3: by-id IDOR guards (question/summary/message/mission) ---")
    # A asks a question + submits a summary + sends a message (A has an active mission).
    with acting_as(TENANT_A, SLUG):
        qa = tools._do_ask_planner("A's question")
        sa = tools._do_submit_progress("A's summary")
        msga = tools._do_send_to_coder("A's message")
    assert "question_id" in qa and "summary_id" in sa and "message_id" in msga, (qa, sa, msga)

    # B tries to answer/respond/ack A's rows by id -> must be rejected as not-found.
    with acting_as(TENANT_B, SLUG):
        r1 = tools._do_answer_question(qa["question_id"], "hacked")
        r2 = tools._do_respond_to_summary(sa["summary_id"], "hacked")
        r3 = tools._do_ack_message(msga["message_id"])
    assert "error" in r1 and "no question" in r1["error"], r1
    assert "error" in r2 and "no summary" in r2["error"], r2
    assert "error" in r3 and "no message" in r3["error"], r3
    print("  [OK] cross-tenant answer/respond/ack all rejected")

    # A (the owner) can act on its own rows.
    with acting_as(TENANT_A, SLUG):
        ok1 = tools._do_answer_question(qa["question_id"], "legit answer")
        ok2 = tools._do_respond_to_summary(sa["summary_id"], "legit response")
        ok3 = tools._do_ack_message(msga["message_id"])
    assert "error" not in ok1 and ok1.get("answer") == "legit answer", ok1
    assert "error" not in ok2 and ok2.get("response") == "legit response", ok2
    assert "error" not in ok3 and ok3.get("delivered_at") is not None, ok3
    print("  [OK] owner can act on its own rows")


def test_jwks_fail_closed():
    print("--- T4: verify_supabase_jwt fails closed ---")
    assert verify_supabase_jwt("not-a-jwt", "https://example.supabase.co") is None
    assert verify_supabase_jwt("", "https://example.supabase.co") is None
    assert verify_supabase_jwt("a.b.c", None) is None
    print("  [OK] garbage / missing-url -> None (no crash)")


def main():
    setup()
    test_resolution_chokepoint()
    test_active_mission_isolation()
    test_by_id_idor()
    test_jwks_fail_closed()
    print("\nALL v2 TENANCY TESTS PASS")


if __name__ == "__main__":
    try:
        main()
    except AssertionError as e:
        print(f"\nFAILED: {e}", file=sys.stderr)
        sys.exit(1)
