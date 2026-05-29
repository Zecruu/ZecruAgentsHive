"""Provision (or update) the AgentsHive admin user in Supabase. SUPERVISED.

Creates the operator's Supabase Auth user via the Admin API (service_role) with
email confirmed and app_metadata = { role: "admin", plan: "pro_unlimited" } so
role + plan ride inside the verified JWT — the server's is_admin() check and the
pro_unlimited gate bypass need no DB round-trip.

Idempotent: if the email already exists, the user is UPDATED in place (password,
app_metadata, email_confirm) rather than erroring.

Secrets: the password is NEVER hardcoded or committed — it is read from --password
or the ADMIN_PASSWORD env var at invocation time only, and never printed.

Usage:
    # reads SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY from env (.env)
    ADMIN_PASSWORD='...' python scripts/create_admin.py --email nomnk5138@gmail.com
    #   or
    python scripts/create_admin.py --email nomnk5138@gmail.com --password '...'
"""

import argparse
import os
import sys

import httpx

ADMIN_APP_METADATA = {"role": "admin", "plan": "pro_unlimited"}


def _find_user_by_email(base: str, headers: dict, email: str) -> dict | None:
    # Supabase admin list-users is paginated; page through until we find the email.
    page = 1
    while True:
        r = httpx.get(f"{base}/auth/v1/admin/users", params={"page": page, "per_page": 200}, headers=headers, timeout=15)
        r.raise_for_status()
        body = r.json()
        users = body.get("users", body if isinstance(body, list) else [])
        for u in users:
            if (u.get("email") or "").lower() == email.lower():
                return u
        if not users or len(users) < 200:
            return None
        page += 1


def main() -> int:
    ap = argparse.ArgumentParser(description="Create/update the AgentsHive Supabase admin user")
    ap.add_argument("--email", default=os.environ.get("ADMIN_EMAIL", ""), help="admin email")
    ap.add_argument("--password", default=os.environ.get("ADMIN_PASSWORD", ""), help="admin password (or ADMIN_PASSWORD env)")
    args = ap.parse_args()

    base = (os.environ.get("SUPABASE_URL", "").rstrip("/"))
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
    if not base or not key:
        print("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set (.env).", file=sys.stderr)
        return 2
    if not args.email or not args.password:
        print("--email and --password (or ADMIN_EMAIL/ADMIN_PASSWORD) are required.", file=sys.stderr)
        return 2

    headers = {"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json"}

    existing = _find_user_by_email(base, headers, args.email)
    if existing is not None:
        uid = existing["id"]
        r = httpx.put(
            f"{base}/auth/v1/admin/users/{uid}",
            headers=headers,
            json={
                "password": args.password,
                "email_confirm": True,
                "app_metadata": ADMIN_APP_METADATA,
            },
            timeout=15,
        )
        r.raise_for_status()
        u = r.json()
        action = "updated"
    else:
        r = httpx.post(
            f"{base}/auth/v1/admin/users",
            headers=headers,
            json={
                "email": args.email,
                "password": args.password,
                "email_confirm": True,
                "app_metadata": ADMIN_APP_METADATA,
            },
            timeout=15,
        )
        r.raise_for_status()
        u = r.json()
        action = "created"

    meta = u.get("app_metadata", {})
    print(f"admin user {action}: email={u.get('email')} sub={u.get('id')} "
          f"role={meta.get('role')} plan={meta.get('plan')} email_confirmed={bool(u.get('email_confirmed_at') or u.get('confirmed_at'))}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except httpx.HTTPStatusError as e:
        print(f"\nFAILED: HTTP {e.response.status_code}: {e.response.text[:300]}", file=sys.stderr)
        sys.exit(1)
