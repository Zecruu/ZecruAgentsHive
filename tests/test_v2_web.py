"""v2.x companion webapp API (in-process).

Exercises the /web helper layer (do_web_send / fetch_web_inbound / do_web_ack /
do_web_relay / upsert_presence / list_agents / fetch_conversation) with TENANT_/
IDENTITY_CONTEXT set the way the Supabase-JWT middleware would. The security
property — a web client can only ever reach its OWN tenant's agents — is the #1
assertion (T2): tenant A's web message must never surface in tenant B's inbound.

Run:
    PYTHONPATH=src python tests/test_v2_web.py
"""

import os
import sys

DB = os.environ.get("AGENTSHIVE_TEST_DB", "sqlite:///./agentshive_v2_web_test.db")
assert DB.startswith("sqlite"), f"refusing non-sqlite test DB: {DB}"

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if os.path.isdir(_SRC) and _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from contextlib import contextmanager  # noqa: E402

from sqlmodel import Session  # noqa: E402

from agentshive import web  # noqa: E402
from agentshive.config import Settings  # noqa: E402
from agentshive.db import Project, get_engine, init_engine  # noqa: E402
from agentshive.tenant import IDENTITY_CONTEXT, TENANT_CONTEXT  # noqa: E402

TENANT_A = "web-tenant-a"
TENANT_B = "web-tenant-b"
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


def _make_project(tenant, slug):
    with Session(get_engine()) as s:
        s.add(Project(tenant_id=tenant, slug=slug, name=f"{tenant}-proj"))
        s.commit()


def setup():
    path = DB.replace("sqlite:///", "").replace("sqlite://", "")
    if path and path != ":memory:" and os.path.exists(path):
        os.remove(path)
    init_engine(Settings(api_key="k", database_url=DB, port=8000,
                         poll_interval_seconds=0.05, tool_block_timeout_seconds=1, supabase_url=None))
    _make_project(TENANT_A, SLUG)
    _make_project(TENANT_B, SLUG)


def test_send_inbound_ack_at_least_once():
    print("--- T1: send -> inbound (at-least-once) -> ack ---")
    with acting_as(TENANT_A):
        sent = web.do_web_send(SLUG, "agent-1", "hello from web")
        assert "message_id" in sent, sent
        mid = sent["message_id"]
        inb1 = web.fetch_web_inbound()
        assert any(m["message_id"] == mid for m in inb1), "inbound should return the unacked message"
        inb2 = web.fetch_web_inbound()
        assert any(m["message_id"] == mid for m in inb2), "still returned until acked (at-least-once)"
        web.do_web_ack(mid)
        inb3 = web.fetch_web_inbound()
        assert not any(m["message_id"] == mid for m in inb3), "acked message must not be redelivered"
    print("  [OK] unacked redelivered, acked drained")


def test_tenant_isolation():
    print("--- T2 (SECURITY): tenant A's web message never reaches tenant B ---")
    with acting_as(TENANT_A):
        a_mid = web.do_web_send(SLUG, "agent-1", "A-only secret")["message_id"]
    with acting_as(TENANT_B):
        b_inbound = web.fetch_web_inbound()
        assert not any(m["message_id"] == a_mid for m in b_inbound), "CROSS-TENANT LEAK: A's msg in B's inbound!"
        # B cannot ack A's message either
        r = web.do_web_ack(a_mid)
        assert "error" in r, "B must not be able to ack A's message"
    print("  [OK] no cross-tenant inbound; cross-tenant ack rejected")


def test_relay_correlation_and_conversation():
    print("--- T3: relay correlates to parent; conversation is tenant-scoped ---")
    with acting_as(TENANT_A):
        sent = web.do_web_send(SLUG, "agent-1", "question?")
        parent = sent["message_id"]
        relayed = web.do_web_relay(parent, SLUG, "agent-1", "answer.")
        assert relayed.get("parent_id") == parent, relayed
        convo = web.fetch_conversation(SLUG, "agent-1")
        bodies = [(m["direction"], m["body"]) for m in convo]
        assert ("web_to_agent", "question?") in bodies and ("agent_to_web", "answer.") in bodies, bodies
    with acting_as(TENANT_B):
        assert web.fetch_conversation(SLUG, "agent-1") == [], "B's same-slug project must be isolated"
    print("  [OK] parent correlation + tenant-scoped conversation")


def test_presence():
    print("--- T4: presence roster tenant-scoped + online ---")
    with acting_as(TENANT_A):
        web.upsert_presence(SLUG, [{"agent_key": "agent-1", "label": "Planner", "role": "hivemind", "cli": "claude", "status": "ready"}])
        agents = web.list_agents()
        assert any(a["agent_key"] == "agent-1" and a["online"] for a in agents), agents
    with acting_as(TENANT_B):
        assert web.list_agents() == [], "B sees none of A's agents"
    print("  [OK] roster online + tenant-isolated")


def main():
    setup()
    test_send_inbound_ack_at_least_once()
    test_tenant_isolation()
    test_relay_correlation_and_conversation()
    test_presence()
    print("\nALL v2 WEB TESTS PASS")


if __name__ == "__main__":
    try:
        main()
    except AssertionError as e:
        print(f"\nFAILED: {e}", file=sys.stderr)
        sys.exit(1)
