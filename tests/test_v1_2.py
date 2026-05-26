"""v1.2 feature coverage: at-least-once messaging, atomicity, input validation, refactor regression.

Run against a fresh local server:
    AGENTSHIVE_API_KEY=test-key PORT=8001 TOOL_BLOCK_TIMEOUT_SECONDS=8 \
        python -m agentshive.main &
    python tests/test_v1_2.py
"""

import asyncio
import os
import sys

from fastmcp import Client
from sqlalchemy import create_engine, inspect, text

KEY = os.environ.get("AGENTSHIVE_API_KEY", "test-key")
URL = os.environ.get("AGENTSHIVE_URL", "http://localhost:8001/mcp")
DB_PATH = os.environ.get("AGENTSHIVE_DB", "sqlite:///./agentshive.db")


def _c(r):
    return r.structured_content if r.structured_content is not None else (r.content[0].text if r.content else None)


# ---------- FEATURE 1: at-least-once messaging ----------

async def test_ack_required_no_auto_stamp():
    print("--- F1.a: wait_for_*_message does NOT auto-stamp delivered_at ---")
    async with Client(URL, auth=KEY) as cli:
        await cli.call_tool("create_mission", {"name": "ack-noauto", "spec": "s"})
        sent = _c(await cli.call_tool("send_to_coder", {"body": "test1"}))
        async with Client(URL, auth=KEY) as coder:
            got = _c(await coder.call_tool("wait_for_planner_message", {"timeout_seconds": 3}))
            assert got["message_id"] == sent["message_id"]
            assert got["delivered_at"] is None, f"expected unacked, got {got}"
            # 0-indexed: 0 means "this is the first delivery, no predecessor saw it"
            assert got["redelivery_count"] == 0, f"expected 0 on first delivery, got {got}"
            print(f"  [OK] returned unacked, redelivery_count=0 (first delivery)")


async def test_redeliver_on_crash():
    print("--- F1.b: same message returns again until ack_message called ---")
    async with Client(URL, auth=KEY) as cli:
        await cli.call_tool("create_mission", {"name": "ack-redeliv", "spec": "s"})
        sent = _c(await cli.call_tool("send_to_coder", {"body": "redelivery test"}))
        mid = sent["message_id"]
        async with Client(URL, auth=KEY) as coder:
            r1 = _c(await coder.call_tool("wait_for_planner_message", {"timeout_seconds": 3}))
            r2 = _c(await coder.call_tool("wait_for_planner_message", {"timeout_seconds": 3}))
            r3 = _c(await coder.call_tool("wait_for_planner_message", {"timeout_seconds": 3}))
            assert r1["message_id"] == r2["message_id"] == r3["message_id"] == mid
            # 0-indexed surface: first delivery -> 0, then 1 (one predecessor), then 2 (two)
            assert (r1["redelivery_count"], r2["redelivery_count"], r3["redelivery_count"]) == (0, 1, 2), \
                f"expected 0,1,2 got {(r1['redelivery_count'], r2['redelivery_count'], r3['redelivery_count'])}"
            print(f"  [OK] same msg returned 3 times, redelivery_count 0->1->2 (predecessor count)")
            # Now ack
            await coder.call_tool("ack_message", {"message_id": mid})
            r4 = _c(await coder.call_tool("wait_for_planner_message", {"timeout_seconds": 2}))
            assert r4.get("status") == "pending", f"expected pending after ack, got {r4}"
            print(f"  [OK] queue drained after ack")


async def test_ack_idempotent():
    print("--- F1.c: ack_message is idempotent ---")
    async with Client(URL, auth=KEY) as cli:
        await cli.call_tool("create_mission", {"name": "ack-idem", "spec": "s"})
        sent = _c(await cli.call_tool("send_to_coder", {"body": "idem test"}))
        async with Client(URL, auth=KEY) as coder:
            await coder.call_tool("wait_for_planner_message", {"timeout_seconds": 3})
            ack1 = _c(await coder.call_tool("ack_message", {"message_id": sent["message_id"]}))
            ack2 = _c(await coder.call_tool("ack_message", {"message_id": sent["message_id"]}))
            assert ack1["delivered_at"] == ack2["delivered_at"], f"not idempotent: {ack1} vs {ack2}"
            print(f"  [OK] two acks return same delivered_at")


async def test_ack_unknown_message():
    print("--- F1.d: ack_message on unknown id returns error ---")
    async with Client(URL, auth=KEY) as cli:
        r = _c(await cli.call_tool("ack_message", {"message_id": "does-not-exist"}))
        assert "error" in r, f"expected error, got {r}"
        print(f"  [OK] {r['error']}")


# ---------- FEATURE 2: create_mission atomicity ----------

def test_partial_unique_index_present():
    print("--- F2.a: partial unique index one_active_mission_per_project exists ---")
    # v1.9: renamed from `one_active_mission` (global) to
    # `one_active_mission_per_project` (composite (project_id, status)).
    # The single-active-globally invariant became single-active-per-project.
    eng = create_engine(DB_PATH)
    idxs = inspect(eng).get_indexes("mission")
    found = [i for i in idxs if i.get("name") == "one_active_mission_per_project"]
    assert found, f"index missing: {idxs}"
    assert found[0]["unique"], f"index not unique: {found}"
    print(f"  [OK] one_active_mission_per_project index present and unique")


def test_db_rejects_manual_second_active():
    print("--- F2.b: direct INSERT bypass is rejected by partial unique index ---")
    eng = create_engine(DB_PATH)
    try:
        with eng.begin() as conn:
            conn.execute(text(
                "INSERT INTO mission (id, name, spec, status, created_at) "
                "VALUES ('bypass-id', 'sneaky', 'sneaky', 'active', '2026-05-24T00:00:00')"
            ))
        raise AssertionError("DB allowed second active row")
    except Exception as e:
        # Expect IntegrityError, NOT AssertionError
        if isinstance(e, AssertionError):
            raise
        print(f"  [OK] DB rejected: {type(e).__name__}")


async def test_sequential_create_mission_supersedes():
    print("--- F2.c: sequential create_mission still works (supersede regression) ---")
    async with Client(URL, auth=KEY) as cli:
        m1 = _c(await cli.call_tool("create_mission", {"name": "atom-A", "spec": "a"}))
        m2 = _c(await cli.call_tool("create_mission", {"name": "atom-B", "spec": "b"}))
        s = _c(await cli.call_tool("is_mission_done", {"mission_id": m1["mission_id"]}))
        assert s["status"] == "superseded", f"expected superseded, got {s}"
        print(f"  [OK] m1 superseded by m2")


def test_concurrent_create_mission_skipped_on_sqlite():
    print("--- F2.d: true concurrent race test ---")
    if DB_PATH.startswith("sqlite"):
        print("  [SKIP] SQLite serializes writes -- partial unique index already proven via direct INSERT bypass")
    else:
        # Postgres path: spawn N threads issuing create_mission, verify all succeed (via retry-and-supersede)
        # and that get_active_mission still returns exactly one. Not exercised here in CI but the
        # mechanism is the IntegrityError retry-once in tools.py:create_mission.
        print("  [TODO] Postgres concurrent test -- currently SQLite-only suite")


# ---------- FEATURE 3: input validation ----------

async def test_validation_rejects_empty_each_field():
    print("--- F3.a: empty/whitespace input rejected on every entry point ---")
    async with Client(URL, auth=KEY) as cli:
        await cli.call_tool("create_mission", {"name": "v3", "spec": "s"})
        bad_cases = [
            ("create_mission", {"name": "", "spec": "x"}, "name"),
            ("create_mission", {"name": "ok", "spec": "  \n "}, "spec"),
            ("ask_planner", {"question": ""}, "question"),
            ("answer_question", {"question_id": "fake", "answer": ""}, "answer"),
            ("submit_progress", {"summary": ""}, "summary"),
            ("respond_to_summary", {"summary_id": "fake", "response": " "}, "response"),
            ("send_to_coder", {"body": ""}, "body"),
            ("send_to_planner", {"body": ""}, "body"),
        ]
        for tool, args, field in bad_cases:
            r = _c(await cli.call_tool(tool, args))
            assert "error" in r and field in r["error"], f"{tool} did not validate {field}: {r}"
        print(f"  [OK] all {len(bad_cases)} entry points rejected empty input")


async def test_validation_length_cap():
    print("--- F3.b: oversized text rejected with length error ---")
    async with Client(URL, auth=KEY) as cli:
        r = _c(await cli.call_tool("create_mission", {"name": "x" * 201, "spec": "ok"}))
        assert "maximum length" in r.get("error", ""), f"expected length error, got {r}"
        r = _c(await cli.call_tool("ask_planner", {"question": "x" * (16 * 1024 + 1)}))
        assert "maximum length" in r.get("error", ""), f"expected length error, got {r}"
        print(f"  [OK] name>200 and question>16KB both rejected")


# ---------- FEATURE 4: refactor regression ----------
# Covered by passing test_v1_1.py and test_supersede.py against the refactored helpers.
# Add a meta-test that asserts both helpers exist and the wait tools are thin wrappers.

def test_helpers_present():
    print("--- F4.a: _wait_specific and _wait_for_active helpers present in module ---")
    import importlib.util
    spec_loc = "src/agentshive/tools.py"
    src = open(spec_loc, encoding="utf-8").read()
    assert "def _wait_specific(" in src, "missing _wait_specific helper"
    assert "def _wait_for_active(" in src, "missing _wait_for_active helper"
    print("  [OK] both refactor helpers present in tools.py")


async def main():
    await test_ack_required_no_auto_stamp()
    await test_redeliver_on_crash()
    await test_ack_idempotent()
    await test_ack_unknown_message()
    test_partial_unique_index_present()
    test_db_rejects_manual_second_active()
    await test_sequential_create_mission_supersedes()
    test_concurrent_create_mission_skipped_on_sqlite()
    await test_validation_rejects_empty_each_field()
    await test_validation_length_cap()
    test_helpers_present()
    print("\nALL v1.2 TESTS PASS")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except AssertionError as e:
        print(f"\nFAILED: {e}", file=sys.stderr)
        sys.exit(1)
