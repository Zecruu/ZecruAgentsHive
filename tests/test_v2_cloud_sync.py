"""v2.x Cloud Sync (opt-in, privacy-first) — server slice (in-process).

Exercises the entitlement gate + transcript push/pull helpers with TENANT_/
IDENTITY_CONTEXT set the way the Supabase-JWT middleware would. The #1 property
is privacy + isolation:
  - T1: entitlement gate — a non-entitled tenant CANNOT push/pull and NOTHING is
        written to the transcript store (privacy-first default).
  - T2: push/pull round-trip + incremental cursor + UUID-keyed LWW upsert.
  - T3 (SECURITY): tenant B can NEVER read or overwrite tenant A's transcripts.

Run:
    PYTHONPATH=src python tests/test_v2_cloud_sync.py
"""

import os
import sys

DB = os.environ.get("AGENTSHIVE_TEST_DB", "sqlite:///./agentshive_v2_cloudsync_test.db")
assert DB.startswith("sqlite"), f"refusing non-sqlite test DB: {DB}"

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if os.path.isdir(_SRC) and _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from contextlib import contextmanager  # noqa: E402

from sqlmodel import Session, select  # noqa: E402

from agentshive import web  # noqa: E402
from agentshive.config import Settings  # noqa: E402
from agentshive.db import (  # noqa: E402
    PLAN_PRO_UNLIMITED,
    SyncedMessage,
    Tenant,
    get_engine,
    get_or_create_tenant,
    init_engine,
)
from agentshive.tenant import IDENTITY_CONTEXT, TENANT_CONTEXT  # noqa: E402

TENANT_FREE = "cs-free"     # real tenant, free plan, cloud_sync off → NOT entitled
TENANT_PRO = "cs-pro"       # pro_unlimited → entitled via plan
TENANT_A = "cs-tenant-a"    # entitled via the cloud_sync flag
TENANT_B = "cs-tenant-b"    # entitled via the cloud_sync flag
SLUG = "p"


@contextmanager
def acting_as(tenant):
    t1 = TENANT_CONTEXT.set(tenant)
    t2 = IDENTITY_CONTEXT.set({"sub": tenant, "email": f"{tenant}@example.com"})
    try:
        yield
    finally:
        TENANT_CONTEXT.reset(t1)
        IDENTITY_CONTEXT.reset(t2)


def _set_entitlement(tenant, plan="free", cloud_sync=False):
    with Session(get_engine()) as s:
        t = get_or_create_tenant(s, tenant)
        t.plan = plan
        t.cloud_sync = cloud_sync
        s.add(t)
        s.commit()


def _count_messages(tenant):
    with Session(get_engine()) as s:
        return len(s.exec(select(SyncedMessage).where(SyncedMessage.tenant_id == tenant)).all())


def setup():
    path = DB.replace("sqlite:///", "").replace("sqlite://", "")
    if path and path != ":memory:" and os.path.exists(path):
        os.remove(path)
    init_engine(Settings(api_key="k", database_url=DB, port=8000,
                         poll_interval_seconds=0.05, tool_block_timeout_seconds=1, supabase_url=None))
    _set_entitlement(TENANT_FREE, plan="free", cloud_sync=False)
    _set_entitlement(TENANT_PRO, plan=PLAN_PRO_UNLIMITED, cloud_sync=False)
    _set_entitlement(TENANT_A, plan="free", cloud_sync=True)
    _set_entitlement(TENANT_B, plan="free", cloud_sync=True)


def _msgs(*pairs):
    """pairs of (uuid, text) → push payload messages with sequential idx."""
    return [
        {"uuid": u, "idx": i, "role": "assistant", "text": txt,
         "tool_calls": None, "tokens": {"input": 10, "output": 5}}
        for i, (u, txt) in enumerate(pairs)
    ]


def test_entitlement_gate():
    print("--- T1: entitlement gate (privacy-first: off => no writes) ---")
    with acting_as(TENANT_FREE):
        me = web.do_web_me()
        assert me["cloud_sync"] is False, me
        push = web.do_sync_push(SLUG, "agent-1", "A", "coder", "claude", _msgs(("u1", "secret")))
        assert push.get("gated") is True, push
        pull = web.do_sync_pull(SLUG)
        assert pull.get("gated") is True, pull
    assert _count_messages(TENANT_FREE) == 0, "PRIVACY LEAK: rows written for a non-entitled tenant!"

    with acting_as(TENANT_PRO):
        assert web.do_web_me()["cloud_sync"] is True, "pro_unlimited must resolve entitled"
        assert web.do_sync_push(SLUG, "agent-1", "P", "coder", "claude", _msgs(("p1", "hi")))["ok"] is True
    print("  [OK] free tenant gated + zero rows; pro_unlimited entitled")


def test_push_pull_roundtrip():
    print("--- T2: push/pull round-trip + UUID LWW + incremental cursor ---")
    with acting_as(TENANT_A):
        web.do_sync_push(SLUG, "agent-1", "Paul", "coder", "claude",
                         _msgs(("a1", "one"), ("a2", "two"), ("a3", "three")))
        pull = web.do_sync_pull(SLUG)
        convo = next(c for c in pull["conversations"] if c["agent_id"] == "agent-1")
        texts = [m["text"] for m in convo["messages"]]
        assert texts == ["one", "two", "three"], texts
        cursor = pull["cursor"]
        assert cursor, "pull must return a cursor"

        # LWW upsert by UUID: re-push a2 with new text — same row, not a dup.
        web.do_sync_push(SLUG, "agent-1", "Paul", "coder", "claude",
                         [{"uuid": "a2", "idx": 1, "role": "assistant", "text": "TWO-edited"}])
        pull2 = web.do_sync_pull(SLUG)
        convo2 = next(c for c in pull2["conversations"] if c["agent_id"] == "agent-1")
        assert [m["text"] for m in convo2["messages"]] == ["one", "TWO-edited", "three"], convo2["messages"]
        assert _count_messages(TENANT_A) == 3, "UUID upsert must not duplicate rows"

        # Incremental: pulling since the FIRST cursor returns only the edited msg.
        inc = web.do_sync_pull(SLUG, since_iso=cursor)
        inc_texts = [m["text"] for c in inc["conversations"] for m in c["messages"]]
        assert inc_texts == ["TWO-edited"], inc_texts
    print("  [OK] round-trip, UUID LWW (no dup), incremental cursor")


def test_cross_tenant_isolation():
    print("--- T3 (SECURITY): tenant B can't read/overwrite tenant A's transcripts ---")
    with acting_as(TENANT_A):
        web.do_sync_push(SLUG, "agent-x", "Paul", "coder", "claude", _msgs(("ax1", "A-only secret")))
    with acting_as(TENANT_B):
        pull = web.do_sync_pull(SLUG)  # same slug, different tenant
        all_uuids = [m["uuid"] for c in pull["conversations"] for m in c["messages"]]
        assert "ax1" not in all_uuids, "CROSS-TENANT LEAK: A's message visible to B!"
        assert not any(c["agent_id"] == "agent-x" for c in pull["conversations"]), "B sees A's conversation!"
        # B writing the SAME uuid must create B's OWN row, never touch A's.
        web.do_sync_push(SLUG, "agent-x", "B", "coder", "claude",
                         [{"uuid": "ax1", "idx": 0, "role": "assistant", "text": "B-overwrite-attempt"}])
    # A's row is untouched.
    with acting_as(TENANT_A):
        a_pull = web.do_sync_pull(SLUG)
        a_msg = next(m for c in a_pull["conversations"] for m in c["messages"] if m["uuid"] == "ax1")
        assert a_msg["text"] == "A-only secret", f"A's row was overwritten cross-tenant: {a_msg}"
    # The (tenant_id, uuid) unique key means A and B each have their own ax1 row.
    with Session(get_engine()) as s:
        rows = s.exec(select(SyncedMessage).where(SyncedMessage.msg_uuid == "ax1")).all()
        tenants = sorted({r.tenant_id for r in rows})
        assert tenants == sorted([TENANT_A, TENANT_B]), tenants
    print("  [OK] no cross-tenant read; per-tenant uuid rows; no cross-tenant overwrite")


def main():
    setup()
    test_entitlement_gate()
    test_push_pull_roundtrip()
    test_cross_tenant_isolation()
    print("\nALL v2 CLOUD SYNC TESTS PASS")


if __name__ == "__main__":
    try:
        main()
    except AssertionError as e:
        print(f"\nFAILED: {e}", file=sys.stderr)
        sys.exit(1)
