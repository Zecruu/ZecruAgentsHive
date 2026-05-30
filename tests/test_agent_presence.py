"""Mission A: AgentPresence + set_my_state / list_agent_states (in-process).

Populated-DB integration style per [feedback-populated-db-integration]. The HTTP
auth integration (ahat_ -> tenant) is verified live separately; here we exercise
the core state-machine + cross-tenant + lazy-promotion logic against a real
SQLite DB seeded with a project + mission row.

Run:
    PYTHONPATH=src python tests/test_agent_presence.py
"""

import os
import sys

DB = os.environ.get("AGENTSHIVE_TEST_DB", "sqlite:///./agentshive_v2_agent_presence_test.db")
# In-process test — no HTTP client (the `localhost`-in-URL guard from
# [feedback-tests-must-assert-localhost] applies to tests that open HTTP clients).
# The sqlite-only guard is the moral equivalent: prevents pointing this run at a
# real Postgres URL by accident.
assert DB.startswith("sqlite"), f"refusing non-sqlite test DB: {DB}"

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if os.path.isdir(_SRC) and _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from contextlib import contextmanager  # noqa: E402
from datetime import datetime, timedelta, timezone  # noqa: E402

from sqlmodel import Session, select  # noqa: E402

from agentshive.config import Settings  # noqa: E402
from agentshive.db import (  # noqa: E402
    AgentPresence, Mission, Project, get_engine, get_or_create_tenant, init_engine,
)
from agentshive.project import DEFAULT_PROJECT_SLUG, PROJECT_CONTEXT  # noqa: E402
from agentshive.tenant import IDENTITY_CONTEXT, LEGACY_TENANT, TENANT_CONTEXT  # noqa: E402
from agentshive.tools import (  # noqa: E402
    AGENT_PRESENCE_DEAD_SECONDS, AGENT_PRESENCE_STALE_SECONDS,
    _do_list_agent_states, _do_set_my_state, _promote_presence_state, _touch_coder,
)


@contextmanager
def ctx(tenant=None, identity=None, project=DEFAULT_PROJECT_SLUG):
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


# --- per-test fixtures: tenant + project + active mission ---
SUB_A = "11111111-2222-3333-4444-aaaaaaaaaaaa"
SUB_B = "22222222-3333-4444-5555-bbbbbbbbbbbb"
PROJECT_SLUG_A = "alpha"
PROJECT_SLUG_B = "beta"


def _seed_project(slug: str, tenant_id: str) -> str:
    """Create (or fetch) a project for `tenant_id` at `slug`. Returns project_id."""
    from agentshive.db import _uuid
    with Session(get_engine()) as s:
        row = s.exec(
            select(Project).where(Project.slug == slug, Project.tenant_id == tenant_id)
        ).first()
        if row is None:
            row = Project(id=_uuid(), tenant_id=tenant_id, slug=slug, name=slug.title())
            s.add(row)
            s.commit()
            s.refresh(row)
        return row.id


def _seed_mission(project_id: str, tenant_id: str, name: str = "test mission") -> str:
    """Create an active mission row for the project + return its id."""
    from agentshive.db import _uuid
    with Session(get_engine()) as s:
        m = Mission(
            id=_uuid(), tenant_id=tenant_id, project_id=project_id,
            name=name, spec="seeded", status="active",
        )
        s.add(m)
        s.commit()
        s.refresh(m)
        return m.id


def setup():
    path = DB.replace("sqlite:///", "").replace("sqlite://", "")
    if path and path != ":memory:" and os.path.exists(path):
        os.remove(path)
    init_engine(Settings(
        api_key="k", database_url=DB, port=8000,
        poll_interval_seconds=0.05, tool_block_timeout_seconds=1,
        supabase_url=None,
    ))
    # Seed both tenants with projects + active missions for cross-tenant testing.
    with Session(get_engine()) as s:
        get_or_create_tenant(s, SUB_A)
        get_or_create_tenant(s, SUB_B)
    a_pid = _seed_project(PROJECT_SLUG_A, SUB_A)
    b_pid = _seed_project(PROJECT_SLUG_B, SUB_B)
    _seed_mission(a_pid, SUB_A)
    _seed_mission(b_pid, SUB_B)


def test_set_my_state_inserts_row():
    print("--- T1: set_my_state inserts row with declared fields ---")
    with ctx(tenant=SUB_A, identity={"sub": SUB_A}, project=PROJECT_SLUG_A):
        res = _do_set_my_state("working", detail="deploying server", expected_seconds=180)
        assert res.get("ok") is True, res
        assert res["state"] == "working"
        assert res["detail"] == "deploying server"
        assert res["expected_done_at"] is not None
    with Session(get_engine()) as s:
        row = s.exec(
            select(AgentPresence).where(
                AgentPresence.tenant_id == SUB_A, AgentPresence.agent_key == "planner",
            )
        ).first()
        assert row is not None and row.state == "working", row
        assert row.role == "planner"
        assert row.detail == "deploying server"
    print("  [OK] row inserted with state/detail/expected_done_at")


def test_set_my_state_updates_existing():
    print("--- T2: set_my_state updates same key; transitioned_at bumps only on state change ---")
    with ctx(tenant=SUB_A, identity={"sub": SUB_A}, project=PROJECT_SLUG_A):
        first = _do_set_my_state("working", detail="step 1")
        # Detail-only update: transitioned_at MUST NOT change.
        second = _do_set_my_state("working", detail="step 2")
        assert second["transitioned_at"] == first["transitioned_at"], (first, second)
        assert second["detail"] == "step 2"
        # State change: transitioned_at MUST change.
        third = _do_set_my_state("idle")
        assert third["transitioned_at"] != first["transitioned_at"], (first, third)
        assert third["state"] == "idle"
    print("  [OK] update semantics correct (detail-only stays; state-change bumps)")


def test_list_agent_states_tenant_scoped():
    print("--- T3: list_agent_states is tenant+project scoped (cross-tenant invisible) ---")
    # Tenant A declares; tenant B's list must NOT include it.
    with ctx(tenant=SUB_A, identity={"sub": SUB_A}, project=PROJECT_SLUG_A):
        _do_set_my_state("working", detail="alpha-side", agent_key="planner")
        listing_a = _do_list_agent_states()
        keys_a = {row["agent_key"] for row in listing_a["agents"]}
        assert "planner" in keys_a, listing_a
    with ctx(tenant=SUB_B, identity={"sub": SUB_B}, project=PROJECT_SLUG_B):
        listing_b = _do_list_agent_states()
        keys_b = {row["agent_key"] for row in listing_b["agents"]}
        # Tenant B has not declared yet; should not see tenant A's planner row.
        assert "planner" not in keys_b or all(
            row.get("detail") != "alpha-side" for row in listing_b["agents"]
        ), listing_b
    print("  [OK] cross-tenant invisibility holds")


def test_coder_tool_upserts_working_row():
    print("--- T4: heartbeat-driven coder tool call upserts a working AgentPresence row ---")
    # Use _touch_coder directly (every coder tool calls it). When no AgentPresence
    # row exists, it should insert with state='working'.
    with ctx(tenant=SUB_A, identity={"sub": SUB_A}, project=PROJECT_SLUG_A):
        with Session(get_engine()) as s:
            _touch_coder(s, coder_id="sexi-lexi")
        with Session(get_engine()) as s:
            row = s.exec(
                select(AgentPresence).where(
                    AgentPresence.tenant_id == SUB_A,
                    AgentPresence.agent_key == "sexi-lexi",
                )
            ).first()
            assert row is not None and row.state == "working", row
            assert row.role == "coder"
    print("  [OK] coder tool call seeded AgentPresence with state=working")


def test_lazy_promotion_stale_then_dead():
    print("--- T5: lazy promotion at read time -- stale at 5 min, dead at 30 min ---")
    now = datetime.now(timezone.utc)
    fresh = now - timedelta(seconds=10)
    stale_age = now - timedelta(seconds=AGENT_PRESENCE_STALE_SECONDS + 60)  # 6 min
    dead_age = now - timedelta(seconds=AGENT_PRESENCE_DEAD_SECONDS + 60)    # 31 min
    # Fresh stays as declared.
    assert _promote_presence_state("working", fresh, now) == "working"
    # Non-idle + stale_age → "stale".
    assert _promote_presence_state("working", stale_age, now) == "stale"
    assert _promote_presence_state("waiting_on_planner", stale_age, now) == "stale"
    # idle stays idle even when old (no noise).
    assert _promote_presence_state("idle", stale_age, now) == "idle"
    # Dead trumps stale regardless of declared state.
    assert _promote_presence_state("working", dead_age, now) == "dead"
    assert _promote_presence_state("idle", dead_age, now) == "dead"
    assert _promote_presence_state("blocked", dead_age, now) == "dead"
    print("  [OK] promotion thresholds enforced")


def test_bad_state_rejected():
    print("--- T6: bad state -> 400-style error dict ---")
    with ctx(tenant=SUB_A, identity={"sub": SUB_A}, project=PROJECT_SLUG_A):
        for bad in ["", "STALE", "dead", "running", "in-progress", "WORKING"]:
            res = _do_set_my_state(bad)
            assert "error" in res, (bad, res)
    print("  [OK] bad/reserved states rejected")


def test_bad_agent_key_rejected():
    print("--- T7: bad agent_key -> 400-style error dict (slug regex) ---")
    with ctx(tenant=SUB_A, identity={"sub": SUB_A}, project=PROJECT_SLUG_A):
        for bad in ["", "  ", "shell;meta", "with space", "UPPER", "-leading-dash",
                    "trailing-dash-", "way-too-long-" + "x" * 60]:
            res = _do_set_my_state("idle", agent_key=bad)
            assert "error" in res, (bad, res)
    print("  [OK] bad agent_keys rejected")


def test_expected_seconds_validation():
    print("--- T8: expected_seconds: positive int, capped at 24h ---")
    with ctx(tenant=SUB_A, identity={"sub": SUB_A}, project=PROJECT_SLUG_A):
        for bad in [-1, 0, "abc", 86_401]:
            res = _do_set_my_state("working", expected_seconds=bad)
            assert "error" in res, (bad, res)
        # Valid edge: exactly 86400 should pass.
        res = _do_set_my_state("working", expected_seconds=86400)
        assert res.get("ok") is True, res
    print("  [OK] expected_seconds validated")


def test_detail_sanitization_and_cap():
    print("--- T9: detail is trimmed + capped to 200 chars + control chars stripped ---")
    with ctx(tenant=SUB_A, identity={"sub": SUB_A}, project=PROJECT_SLUG_A):
        # Whitespace-only -> stored as None.
        res = _do_set_my_state("working", detail="   ")
        assert res["detail"] is None, res
        # Tab/CR control chars stripped; \n kept (allowed).
        res = _do_set_my_state("working", detail="line1\nline2\twith-tab\rEND")
        assert "\t" not in (res["detail"] or "") and "\r" not in (res["detail"] or ""), res
        assert "line1" in res["detail"] and "line2" in res["detail"]
        # 250-char detail clipped to 200.
        res = _do_set_my_state("working", detail="x" * 250)
        assert len(res["detail"]) == 200, len(res["detail"])
    print("  [OK] detail sanitized + capped")


def main():
    setup()
    test_set_my_state_inserts_row()
    test_set_my_state_updates_existing()
    test_list_agent_states_tenant_scoped()
    test_coder_tool_upserts_working_row()
    test_lazy_promotion_stale_then_dead()
    test_bad_state_rejected()
    test_bad_agent_key_rejected()
    test_expected_seconds_validation()
    test_detail_sanitization_and_cap()
    print("ALL OK")


if __name__ == "__main__":
    main()
