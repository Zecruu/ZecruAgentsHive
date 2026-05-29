"""v2.x tenancy migration — SUPERVISED, NOT auto-run.

The additive part of tenancy (nullable tenant_id columns + backfill to the
"legacy" tenant) runs automatically on startup via db._apply_inline_migrations.
The RISKY part — swapping the global UNIQUE(project.slug) constraint to a
per-tenant UNIQUE(tenant_id, slug) — is deliberately NOT auto-run, because on a
live production DB it rewrites a uniqueness invariant the running server depends
on. This script performs that swap, on demand, against an explicitly-named DB.

It is idempotent and additive-first:
  1. Ensure every tenancy table has a tenant_id column, backfilled to "legacy".
  2. Create the composite UNIQUE(tenant_id, slug) index if missing.
  3. Drop the old global unique on project.slug if present.

Usage (run deliberately, with eyes on it):
    # Dry run — report what it WOULD do, change nothing:
    python scripts/migrate_tenancy.py --database-url "<url>"

    # Apply:
    python scripts/migrate_tenancy.py --database-url "<url>" --confirm

The --database-url is REQUIRED and echoed back; there is no default, so you can
never accidentally migrate the wrong database. Never point this at the live
production DB without a backup and a maintenance window.
"""

import argparse
import os
import sys

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if os.path.isdir(_SRC) and _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from sqlalchemy import create_engine, inspect, text  # noqa: E402

from agentshive.config import _normalize_database_url  # noqa: E402
from agentshive.tenant import LEGACY_TENANT  # noqa: E402

TENANCY_TABLES = [
    "project", "mission", "question", "summary", "message",
    "coderheartbeat", "oauthclient", "oauthaccesstoken", "oauthauthorizationcode",
    "oauthrefreshtoken",
]


def _project_slug_unique_indexes(inspector) -> list[str]:
    """Names of UNIQUE indexes on project that cover slug WITHOUT tenant_id —
    i.e. the legacy global-unique constraint we want to drop."""
    out = []
    for ix in inspector.get_indexes("project"):
        cols = list(ix.get("column_names") or [])
        if ix.get("unique") and "slug" in cols and "tenant_id" not in cols:
            out.append(ix["name"])
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="v2.x per-tenant slug uniqueness migration")
    ap.add_argument("--database-url", required=True, help="SQLAlchemy DB URL to migrate (echoed back)")
    ap.add_argument("--confirm", action="store_true", help="actually apply changes (otherwise dry-run)")
    args = ap.parse_args()

    url = _normalize_database_url(args.database_url.strip())
    dialect = "postgresql" if url.startswith("postgresql") else ("sqlite" if url.startswith("sqlite") else "other")
    print(f"Target DB : {url}")
    print(f"Dialect   : {dialect}")
    print(f"Mode      : {'APPLY' if args.confirm else 'DRY RUN (no changes)'}")
    print("-" * 60)

    connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
    engine = create_engine(url, connect_args=connect_args, pool_pre_ping=True)
    inspector = inspect(engine)

    if "project" not in inspector.get_table_names():
        print("No 'project' table — nothing to migrate.")
        return 0

    # 1) additive columns + backfill
    plan = []
    for tbl in TENANCY_TABLES:
        if tbl not in inspector.get_table_names():
            continue
        cols = {c["name"] for c in inspector.get_columns(tbl)}
        if "tenant_id" not in cols:
            plan.append(("add-column", tbl))
        plan.append(("backfill", tbl))

    # 2) composite unique
    existing_idx_names = {ix["name"] for ix in inspector.get_indexes("project")}
    if "uq_project_tenant_slug" not in existing_idx_names:
        plan.append(("create-composite-unique", "project"))

    # 3) drop old global unique(s)
    legacy_unique = _project_slug_unique_indexes(inspector)
    for name in legacy_unique:
        plan.append(("drop-global-unique", name))

    for action, target in plan:
        print(f"  WILL {action:>22}  {target}")
    if not plan:
        print("  (nothing to do — already migrated)")

    if not args.confirm:
        print("\nDry run complete. Re-run with --confirm to apply.")
        return 0

    print("\nApplying...")
    with engine.begin() as conn:
        for action, target in plan:
            if action == "add-column":
                conn.execute(text(f"ALTER TABLE {target} ADD COLUMN tenant_id TEXT"))
            elif action == "backfill":
                conn.execute(
                    text(f"UPDATE {target} SET tenant_id = :t WHERE tenant_id IS NULL"),
                    {"t": LEGACY_TENANT},
                )
            elif action == "create-composite-unique":
                conn.execute(text(
                    "CREATE UNIQUE INDEX IF NOT EXISTS uq_project_tenant_slug "
                    "ON project (tenant_id, slug)"
                ))
            elif action == "drop-global-unique":
                # SQLite: DROP INDEX <name>; Postgres: indexes/constraints both drop
                # by name via DROP INDEX (unique indexes), constraints via ALTER.
                try:
                    conn.execute(text(f"DROP INDEX IF EXISTS {target}"))
                except Exception:
                    conn.execute(text(f"ALTER TABLE project DROP CONSTRAINT IF EXISTS {target}"))
            print(f"  DONE {action:>22}  {target}")

    print("\nMigration applied.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
