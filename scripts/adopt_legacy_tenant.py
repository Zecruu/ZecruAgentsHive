"""v2.x legacy-data adoption — SUPERVISED, NOT auto-run.

The problem: all pre-v2 data lives under tenant_id="legacy" (backfilled by the
additive migration). The first time the operator signs in with Supabase they
become tenant=<their sub> — a FRESH tenant that would see NONE of the existing
"agentshive" project / missions / agents. This script reassigns the legacy
workspace to the operator's real Supabase user so signing in adopts everything.

Approach (b) — DATA RE-TENANTING: UPDATE tenant_id from "legacy" to the given
Supabase sub across every tenancy table. Chosen over a runtime sub->legacy remap
because it leaves the data correctly labeled under a real tenant with no
permanent special-case in the auth path. One-shot, idempotent-ish (re-running
with the same sub is a no-op once no "legacy" rows remain).

After this runs, the legacy SHARED-KEY path will no longer see the adopted data
(it resolves tenant="legacy", now empty) — which is fine, because adoption is
part of the supervised cutover away from the shared key. Run it only once the
operator has signed in and read their user id (sub) from the desktop app.

Usage:
    # Dry run (default) — report counts, change nothing:
    python scripts/adopt_legacy_tenant.py --database-url "<url>" --tenant <supabase-sub>

    # Apply:
    python scripts/adopt_legacy_tenant.py --database-url "<url>" --tenant <supabase-sub> --confirm

--database-url and --tenant are REQUIRED and echoed back. The tenant must look
like a UUID (Supabase sub) and must NOT be the reserved "legacy"/sentinel value.
"""

import argparse
import os
import re
import sys

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if os.path.isdir(_SRC) and _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from sqlalchemy import create_engine, inspect, text  # noqa: E402

from agentshive.config import _normalize_database_url  # noqa: E402
from agentshive.tenant import LEGACY_TENANT, UNAUTHENTICATED_TENANT  # noqa: E402

TENANCY_TABLES = [
    "project", "mission", "question", "summary", "message",
    "coderheartbeat", "oauthclient", "oauthaccesstoken", "oauthauthorizationcode",
    "oauthrefreshtoken",
]

_UUID_RE = re.compile(r"^[0-9a-fA-F-]{20,}$")


def main() -> int:
    ap = argparse.ArgumentParser(description="Adopt the legacy workspace into a Supabase tenant")
    ap.add_argument("--database-url", required=True, help="SQLAlchemy DB URL (echoed back)")
    ap.add_argument("--tenant", required=True, help="target Supabase user id (sub) to adopt legacy data into")
    ap.add_argument("--confirm", action="store_true", help="actually apply (otherwise dry-run)")
    args = ap.parse_args()

    target = args.tenant.strip()
    if target in (LEGACY_TENANT, UNAUTHENTICATED_TENANT) or not _UUID_RE.match(target):
        print(f"refusing: --tenant must be a real Supabase sub (UUID-like), got {target!r}", file=sys.stderr)
        return 2

    url = _normalize_database_url(args.database_url.strip())
    print(f"Target DB : {url}")
    print(f"Adopt into: tenant_id = {target}")
    print(f"Mode      : {'APPLY' if args.confirm else 'DRY RUN (no changes)'}")
    print("-" * 60)

    connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
    engine = create_engine(url, connect_args=connect_args, pool_pre_ping=True)
    inspector = inspect(engine)
    tables = [t for t in TENANCY_TABLES if t in inspector.get_table_names()]

    # Count legacy rows per table first.
    total = 0
    with engine.connect() as conn:
        counts = {}
        for tbl in tables:
            n = conn.execute(
                text(f"SELECT COUNT(*) FROM {tbl} WHERE tenant_id = :t"), {"t": LEGACY_TENANT}
            ).scalar() or 0
            counts[tbl] = n
            total += n
    for tbl, n in counts.items():
        print(f"  {tbl:>26}: {n} legacy row(s) -> {target}")
    print(f"  {'TOTAL':>26}: {total}")

    if total == 0:
        print("\nNothing to adopt (no rows under the legacy tenant).")
        return 0
    if not args.confirm:
        print("\nDry run complete. Re-run with --confirm to apply.")
        return 0

    print("\nApplying...")
    with engine.begin() as conn:
        for tbl in tables:
            res = conn.execute(
                text(f"UPDATE {tbl} SET tenant_id = :new WHERE tenant_id = :old"),
                {"new": target, "old": LEGACY_TENANT},
            )
            print(f"  {tbl:>26}: {res.rowcount} row(s) re-tenanted")
    print(f"\nAdoption complete — legacy workspace now belongs to {target}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
