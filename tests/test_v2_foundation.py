"""v2.x foundation mission (in-process).

The foundation mission is the project's durable north-star goal: seeded from the
FIRST mission, never superseded, tenant+project scoped. Exercised via the same
_do_* helpers the MCP tools delegate to, with TENANT_CONTEXT/PROJECT_CONTEXT set
the way the middleware would.

Run:
    PYTHONPATH=src python tests/test_v2_foundation.py
"""

import os
import sys

DB = os.environ.get("AGENTSHIVE_TEST_DB", "sqlite:///./agentshive_v2_foundation_test.db")
assert DB.startswith("sqlite"), f"refusing non-sqlite test DB: {DB}"

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if os.path.isdir(_SRC) and _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from contextlib import contextmanager  # noqa: E402

from sqlmodel import Session  # noqa: E402

from agentshive import tools  # noqa: E402
from agentshive.config import Settings  # noqa: E402
from agentshive.db import Project, get_engine, init_engine  # noqa: E402
from agentshive.project import PROJECT_CONTEXT  # noqa: E402
from agentshive.tenant import TENANT_CONTEXT  # noqa: E402

TENANT_A = "tenant-a-foundation"
TENANT_B = "tenant-b-foundation"
SLUG = "shared"


@contextmanager
def acting_as(tenant, slug=SLUG):
    t1 = TENANT_CONTEXT.set(tenant)
    t2 = PROJECT_CONTEXT.set(slug)
    try:
        yield
    finally:
        TENANT_CONTEXT.reset(t1)
        PROJECT_CONTEXT.reset(t2)


def _make_project(tenant, slug, name):
    with Session(get_engine()) as s:
        p = Project(tenant_id=tenant, slug=slug, name=name)
        s.add(p); s.commit()


def _foundation():
    with Session(get_engine()) as s:
        pid = tools._project_id(s)
        return tools._foundation_dict(s.get(Project, pid)) if pid else None


def setup():
    path = DB.replace("sqlite:///", "").replace("sqlite://", "")
    if path and path != ":memory:" and os.path.exists(path):
        os.remove(path)
    init_engine(Settings(api_key="k", database_url=DB, port=8000,
                         poll_interval_seconds=0.05, tool_block_timeout_seconds=1, supabase_url=None))


def test_first_mission_seeds_foundation():
    print("--- T1: first mission seeds the foundation ---")
    _make_project(TENANT_A, SLUG, "A")
    with acting_as(TENANT_A):
        assert _foundation() is None
        tools._do_create_mission("Build a game engine", "the north-star spec")
        fd = _foundation()
        assert fd and fd["name"] == "Build a game engine", fd
    print("  [OK] foundation captured from first mission")


def test_foundation_survives_supersede():
    print("--- T2: a new mission does NOT supersede the foundation ---")
    with acting_as(TENANT_A):
        tools._do_create_mission("Fix bug #1", "rotating active mission")
        # active mission rotated, but foundation is unchanged
        fd = _foundation()
        assert fd and fd["name"] == "Build a game engine", fd
        with Session(get_engine()) as s:
            active = tools._active_mission(s)
            assert active.name == "Fix bug #1", active.name
    print("  [OK] foundation persists; active mission rotates independently")


def test_tenant_isolation():
    print("--- T3: foundation is tenant-isolated (same slug) ---")
    _make_project(TENANT_B, SLUG, "B")
    with acting_as(TENANT_B):
        assert _foundation() is None  # B's project has its own (empty) foundation
        tools._do_create_mission("Build an ecommerce site", "B north-star")
        assert _foundation()["name"] == "Build an ecommerce site"
    with acting_as(TENANT_A):
        assert _foundation()["name"] == "Build a game engine", "A's foundation must be untouched by B"
    print("  [OK] tenant A and B foundations are independent")


def test_explicit_refine():
    print("--- T4: set_foundation_mission refines it ---")
    with acting_as(TENANT_A):
        out = tools._do_set_foundation("Game engine v2", "refined goal")
        assert out.get("name") == "Game engine v2", out
        assert _foundation()["name"] == "Game engine v2"
    with acting_as(TENANT_B):
        assert _foundation()["name"] == "Build an ecommerce site", "B unaffected by A's refine"
    print("  [OK] explicit refine works, tenant-scoped")


def main():
    setup()
    test_first_mission_seeds_foundation()
    test_foundation_survives_supersede()
    test_tenant_isolation()
    test_explicit_refine()
    print("\nALL v2 FOUNDATION TESTS PASS")


if __name__ == "__main__":
    try:
        main()
    except AssertionError as e:
        print(f"\nFAILED: {e}", file=sys.stderr)
        sys.exit(1)
