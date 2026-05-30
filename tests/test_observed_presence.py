"""Mission B: observed-presence upsert + set_planner_status (in-process).

Populated-DB integration style per [feedback-populated-db-integration]. The HTTP
auth integration (ahat_ -> tenant -> /api/dashboard/presence) is verified live
separately; here we exercise the core upsert + override semantics + set_planner_status
alias + cross-tenant isolation against a real SQLite DB.

Run:
    PYTHONPATH=src python tests/test_observed_presence.py
"""

import os
import sys

DB = os.environ.get("AGENTSHIVE_TEST_DB", "sqlite:///./agentshive_v2_observed_presence_test.db")
# In-process test — no HTTP client (the `localhost`-in-URL guard from
# [feedback-tests-must-assert-localhost] applies to tests that open HTTP clients).
# The sqlite-only guard is the moral equivalent.
assert DB.startswith("sqlite"), f"refusing non-sqlite test DB: {DB}"

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if os.path.isdir(_SRC) and _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from contextlib import contextmanager  # noqa: E402
from datetime import datetime, timedelta, timezone  # noqa: E402

from sqlmodel import Session, select  # noqa: E402

from agentshive.config import Settings  # noqa: E402
from agentshive.db import (  # noqa: E402
    AgentPresence, Mission, Project, get_engine, get_or_create_tenant, init_engine, _uuid,
)
from agentshive.project import DEFAULT_PROJECT_SLUG, PROJECT_CONTEXT  # noqa: E402
from agentshive.tenant import IDENTITY_CONTEXT, TENANT_CONTEXT  # noqa: E402
from agentshive.tools import (  # noqa: E402
    AGENT_PRESENCE_STALE_SECONDS,
    _do_list_agent_states, _do_publish_observed_presence, _do_set_my_state,
    _do_set_planner_status,
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


SUB_A = "aaaa1111-2222-3333-4444-aaaaaaaaaaaa"
SUB_B = "bbbb1111-2222-3333-4444-bbbbbbbbbbbb"
PROJECT_SLUG_A = "alpha"
PROJECT_SLUG_B = "beta"


def _seed_project(slug: str, tenant_id: str) -> str:
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
    with Session(get_engine()) as s:
        get_or_create_tenant(s, SUB_A)
        get_or_create_tenant(s, SUB_B)
    a_pid = _seed_project(PROJECT_SLUG_A, SUB_A)
    b_pid = _seed_project(PROJECT_SLUG_B, SUB_B)
    _seed_mission(a_pid, SUB_A)
    _seed_mission(b_pid, SUB_B)


def test_publish_inserts_observed_row():
    """POST /api/dashboard/presence body inserts AgentPresence with source='observed'."""
    print("--- T1: publish inserts row with source='observed' ---")
    with ctx(tenant=SUB_A, identity={"sub": SUB_A}, project=PROJECT_SLUG_A):
        now = datetime.now(timezone.utc).isoformat()
        res = _do_publish_observed_presence([
            {"agent_key": "paul", "state": "working", "detail": "streaming response", "observed_at": now},
        ])
        assert res.get("ok") is True and res.get("applied") == 1, res
        listing = _do_list_agent_states()
        rows = listing.get("agents", [])
        paul = next((r for r in rows if r["agent_key"] == "paul"), None)
        assert paul is not None and paul["state"] == "working" and paul["source"] == "observed", paul
        assert paul["detail"] == "streaming response"
        assert paul["role"] == "coder"
    print("  [OK] observed row inserted")


def test_observed_shadows_declared():
    """When both declared + observed exist on the same row, observed wins on read."""
    print("--- T2: observed shadows declared on the same row ---")
    with ctx(tenant=SUB_A, identity={"sub": SUB_A}, project=PROJECT_SLUG_A):
        # First a declaration.
        _do_set_my_state("waiting_on_planner", detail="declared-detail", agent_key="sexi-lexi")
        listing1 = _do_list_agent_states()
        sl1 = next(r for r in listing1["agents"] if r["agent_key"] == "sexi-lexi")
        assert sl1["source"] == "declared" and sl1["state"] == "waiting_on_planner"
        # Now overwrite via observed publish.
        res = _do_publish_observed_presence([
            {"agent_key": "sexi-lexi", "state": "working", "detail": "observed-detail",
             "observed_at": datetime.now(timezone.utc).isoformat()},
        ])
        assert res.get("ok") is True, res
        listing2 = _do_list_agent_states()
        sl2 = next(r for r in listing2["agents"] if r["agent_key"] == "sexi-lexi")
        assert sl2["source"] == "observed" and sl2["state"] == "working", sl2
        assert sl2["detail"] == "observed-detail"
    print("  [OK] observed write shadows the prior declared value")


def test_stale_observed_lazy_promotes():
    """An observed row whose heartbeat has aged past 5 min lazy-promotes to stale."""
    print("--- T3: stale observed row promotes via standard lazy rule ---")
    with ctx(tenant=SUB_A, identity={"sub": SUB_A}, project=PROJECT_SLUG_A):
        # Insert observed row, then backdate the heartbeat to 6 min ago.
        _do_publish_observed_presence([
            {"agent_key": "paul", "state": "working", "detail": "x",
             "observed_at": datetime.now(timezone.utc).isoformat()},
        ])
        backdate = datetime.now(timezone.utc) - timedelta(seconds=AGENT_PRESENCE_STALE_SECONDS + 60)
        with Session(get_engine()) as s:
            row = s.exec(
                select(AgentPresence).where(
                    AgentPresence.tenant_id == SUB_A, AgentPresence.agent_key == "paul",
                )
            ).first()
            row.last_heartbeat_at = backdate
            s.add(row); s.commit()
        listing = _do_list_agent_states()
        paul = next(r for r in listing["agents"] if r["agent_key"] == "paul")
        assert paul["state"] == "stale", paul
        # source stays "observed" — it's the row's source; the EFFECTIVE state was promoted.
        assert paul["source"] == "observed"
        # declared_state reflects what was on the row pre-promotion (last observed value).
        assert paul["declared_state"] == "working"
    print("  [OK] stale observed row promotes to 'stale' at read time")


def test_cross_tenant_isolation():
    """Tenant A's observed publish doesn't affect tenant B's rows."""
    print("--- T4: cross-tenant: tenant A publish invisible to tenant B ---")
    with ctx(tenant=SUB_A, identity={"sub": SUB_A}, project=PROJECT_SLUG_A):
        _do_publish_observed_presence([
            {"agent_key": "alpha-coder", "state": "working",
             "observed_at": datetime.now(timezone.utc).isoformat()},
        ])
    with ctx(tenant=SUB_B, identity={"sub": SUB_B}, project=PROJECT_SLUG_B):
        listing = _do_list_agent_states()
        keys = {r["agent_key"] for r in listing["agents"]}
        assert "alpha-coder" not in keys, listing
    print("  [OK] tenant B does not see tenant A's observed row")


def test_bad_inputs_rejected():
    """Bad state / agent_key / observed_at skew → per-entry error, not 500."""
    print("--- T5: bad entries rejected with error strings (not crash) ---")
    with ctx(tenant=SUB_A, identity={"sub": SUB_A}, project=PROJECT_SLUG_A):
        future = (datetime.now(timezone.utc) + timedelta(seconds=120)).isoformat()
        past = (datetime.now(timezone.utc) - timedelta(seconds=120)).isoformat()
        res = _do_publish_observed_presence([
            {"agent_key": "Bad Key With Spaces", "state": "working"},
            {"agent_key": "ok-coder", "state": "blocked"},  # blocked not in observed bucket
            {"agent_key": "ok-coder", "state": "working", "observed_at": future},
            {"agent_key": "ok-coder", "state": "working", "observed_at": past},
            {"agent_key": "ok-coder", "state": "working", "observed_at": "not-an-iso"},
        ])
        # Each entry above is invalid; we expect zero applied + 5 errors.
        assert res.get("applied", 0) == 0, res
        assert len(res.get("errors", [])) == 5, res
    print("  [OK] bad inputs collected as errors, no row applied")


def test_set_planner_status_aliases_set_my_state():
    """text non-empty → working+detail; empty/None → idle. Same row shape."""
    print("--- T6: set_planner_status is a thin alias on set_my_state ---")
    with ctx(tenant=SUB_A, identity={"sub": SUB_A}, project=PROJECT_SLUG_A):
        # text=non-empty -> working
        res = _do_set_planner_status("deploying server", expected_seconds=120)
        assert res.get("ok") is True and res["state"] == "working", res
        assert res["agent_key"] == "planner"
        assert res["detail"] == "deploying server"
        assert res["expected_done_at"] is not None
        # text=None -> idle, clear detail
        res2 = _do_set_planner_status(None)
        assert res2.get("ok") is True and res2["state"] == "idle", res2
        assert res2["detail"] is None
        # text="   " (whitespace only) -> idle as well
        res3 = _do_set_planner_status("   ")
        assert res3.get("ok") is True and res3["state"] == "idle", res3
    print("  [OK] set_planner_status routes correctly to set_my_state")


def test_declared_then_observed_then_declared_again():
    """A declared write AFTER an observed one re-stamps source='declared'."""
    print("--- T7: declared-after-observed re-stamps source ---")
    with ctx(tenant=SUB_A, identity={"sub": SUB_A}, project=PROJECT_SLUG_A):
        _do_publish_observed_presence([
            {"agent_key": "round-trip", "state": "working",
             "observed_at": datetime.now(timezone.utc).isoformat()},
        ])
        _do_set_my_state("waiting_on_user", detail="re-declared", agent_key="round-trip")
        listing = _do_list_agent_states()
        rt = next(r for r in listing["agents"] if r["agent_key"] == "round-trip")
        assert rt["source"] == "declared", rt
        assert rt["state"] == "waiting_on_user"
        assert rt["detail"] == "re-declared"
    print("  [OK] declared write after observed flips source back")


def main():
    setup()
    test_publish_inserts_observed_row()
    test_observed_shadows_declared()
    test_stale_observed_lazy_promotes()
    test_cross_tenant_isolation()
    test_bad_inputs_rejected()
    test_set_planner_status_aliases_set_my_state()
    test_declared_then_observed_then_declared_again()
    print("ALL OK")


if __name__ == "__main__":
    main()
