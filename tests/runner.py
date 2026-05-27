"""v1.17 — sequential test runner.

Runs every AgentsHive test suite sequentially against a single localhost
server. Required because legacy suites (v1.1, v1.2, v1.3, supersede,
inbox, dashboard*, oauth) all assume sole ownership of the default
project's active-mission state. Running two of them in parallel via
asyncio.gather or background tasks causes state contamination — a
question created by suite A leaks into suite B's wait_for_next_question
assertion, etc.

v1.13's Coder caught this the hard way when their first test pass ran
batches concurrently and saw a spurious test_v1_1 F1.a failure.

This runner serializes the invocations: each suite finishes before the
next starts, so no two suites are reading/writing the default project's
mission state at the same time.

USAGE

  # Start a single server first (don't restart between suites)
  AGENTSHIVE_API_KEY=test-key PORT=8000 \\
      DATABASE_URL=sqlite:///./agentshive_test_runner.db \\
      AGENTSHIVE_BASE_URL=http://localhost:8000 \\
      TOOL_BLOCK_TIMEOUT_SECONDS=2 \\
      python -m agentshive.main &

  # Then run all suites against it sequentially:
  AGENTSHIVE_API_KEY=test-key AGENTSHIVE_BASE=http://localhost:8000 \\
      python tests/runner.py

  # Or run a single suite:
  python tests/runner.py test_v1_16_scope_guard

  # Or skip slow suites:
  python tests/runner.py --skip test_oauth test_dashboard_sse

EXIT CODE

  0 if every suite passed, 1 if any failed. Per-suite stdout/stderr is
  passed through so you see the same output as running each by hand.

v1.18+ may revisit Option A (refactor every suite to use its own scoped
project so they CAN run in parallel). For now this runner solves the
"parallel runs are flaky" problem with minimal disruption.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

# All test suites in the order they should run. Order matters slightly:
# smoke goes first (sanity check) and the older default-project-coupled
# suites go before the newer scoped suites so a regression in the
# foundational tools shows up immediately.
SUITES = [
    "smoke",
    "test_supersede",
    "test_v1_1",
    "test_v1_2",
    "test_v1_3",
    "test_dashboard",
    "test_dashboard_writes",
    "test_dashboard_sse",
    "test_oauth",
    "test_inbox",
    "test_projects",
    "test_v1_11_coder_id",
    "test_v1_12_project_info",
    "test_v1_13_multicoder_ux",
    "test_v1_15_cross_device",
    "test_v1_16_scope_guard",
]


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Sequential AgentsHive test-suite runner",
    )
    parser.add_argument(
        "suite", nargs="?", default=None,
        help="Run only this single suite (e.g., test_v1_16_scope_guard). "
             "Omit to run all suites.",
    )
    parser.add_argument(
        "--skip", nargs="+", default=[],
        help="Suite names to skip (e.g., --skip test_oauth test_dashboard_sse).",
    )
    parser.add_argument(
        "--list", action="store_true",
        help="Print the suite list and exit.",
    )
    parser.add_argument(
        "--python", default=None,
        help="Python interpreter to invoke each suite with. "
             "Defaults to the current interpreter (sys.executable).",
    )
    args = parser.parse_args()

    if args.list:
        for s in SUITES:
            print(s)
        return 0

    py = args.python or sys.executable
    tests_dir = Path(__file__).resolve().parent

    if args.suite is not None:
        if args.suite not in SUITES:
            print(f"ERROR: unknown suite {args.suite!r}. Use --list to see options.",
                  file=sys.stderr)
            return 2
        suites = [args.suite]
    else:
        suites = [s for s in SUITES if s not in args.skip]

    # Run each suite as a subprocess so a hard crash in one doesn't kill
    # the runner. Pass through env so AGENTSHIVE_BASE etc. work.
    results: list[tuple[str, int, float]] = []
    overall_start = time.monotonic()
    for s in suites:
        suite_path = tests_dir / f"{s}.py"
        if not suite_path.is_file():
            print(f"  [SKIP] {s} (file not found)")
            results.append((s, -1, 0.0))
            continue
        print(f"=== {s} ===", flush=True)
        start = time.monotonic()
        rc = subprocess.call([py, str(suite_path)], env=os.environ)
        elapsed = time.monotonic() - start
        status = "PASS" if rc == 0 else f"FAIL (exit {rc})"
        print(f"  -> {status} in {elapsed:.1f}s", flush=True)
        results.append((s, rc, elapsed))

    total = time.monotonic() - overall_start
    print()
    print("=" * 56)
    print(f"TOTAL: {len([r for r in results if r[1] == 0])}/{len(results)} suites passed in {total:.1f}s")
    print("=" * 56)
    # ASCII markers — keep portable on Windows cp1252 consoles. ✓/✗/?
    # would crash the summary print on default Windows shells.
    for s, rc, elapsed in results:
        marker = "OK " if rc == 0 else ("-- " if rc == -1 else "X  ")
        print(f"  {marker}{s:35s} {elapsed:5.1f}s  exit={rc}")

    failed = [s for s, rc, _ in results if rc not in (0, -1)]
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
