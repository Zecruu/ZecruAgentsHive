"""v2.x multi-tenancy primitives.

Tenancy pivots on ONE chokepoint: project resolution. `Project` rows carry a
`tenant_id`; `_project_id()` in tools.py resolves `(tenant_id, slug)` → project
id, so every child query that roots at a project is transitively tenant-isolated
through the FK chain. The single place that leaks — loading a child row by its
OWN global id — is closed by the denormalized `tenant_id` column on each child
table, which is a LIVE enforced filter on by-id access (see `tenant_scoped_get`).

Identity sources:
  - Supabase JWT (verified via JWKS) → tenant_id = the token's `sub` (a UUID).
  - Legacy shared AGENTSHIVE_API_KEY → tenant_id = LEGACY_TENANT. Preserves the
    pre-v2 single-tenant behavior so the legacy key keeps seeing today's data.

The TENANT_CONTEXT ContextVar is set per-request by TenantContextMiddleware
(auth.py) and read by tools.py / dashboard.py. Default is LEGACY_TENANT so
trusted in-process callers (tests, direct dashboard handler calls) behave like
the pre-tenancy server; HTTP requests are always re-stamped by the middleware
based on which auth method actually succeeded.
"""

import os
from contextvars import ContextVar
from typing import Optional

# Reserved tenant for all pre-v2 / legacy-shared-key traffic. Supabase subs are
# UUIDs, so a real tenant can never collide with this literal.
LEGACY_TENANT = "legacy"

# Sentinel for a request that presented a bearer token which was NEITHER the
# shared key NOR a valid Supabase JWT. It must never match a real Project.tenant_id
# so such a request (if it somehow reaches a tool) resolves to no project. Such
# requests are already 401'd upstream by BearerAuth/the SDK; this is defense in
# depth so an invalid token never silently inherits the LEGACY default.
UNAUTHENTICATED_TENANT = "__unauthenticated__"

TENANT_CONTEXT: ContextVar[str] = ContextVar("tenant", default=LEGACY_TENANT)

# The verified identity of the current request (Supabase JWT claims subset), or
# None for legacy-key / cookie / in-process callers. Set by TenantContextMiddleware
# alongside TENANT_CONTEXT. Read by is_admin() and the /admin router.
IDENTITY_CONTEXT: ContextVar[Optional[dict]] = ContextVar("identity", default=None)


def current_tenant() -> str:
    """The current request's tenant id. Always non-empty (defaults to LEGACY_TENANT
    for in-process callers that ran without the middleware)."""
    return TENANT_CONTEXT.get()


def current_identity() -> Optional[dict]:
    """The current request's verified Supabase identity dict, or None."""
    return IDENTITY_CONTEXT.get()


def is_admin() -> bool:
    """True iff the current request is an authenticated admin.

    Verified-JWT based (UI hiding is NOT security): admin if the token's
    app_metadata.role == "admin" OR the token's email == ADMIN_EMAIL env. The
    legacy shared key and cookie sessions are NOT admins (no verified identity).
    """
    ident = current_identity()
    if not ident:
        return False
    if ident.get("role") == "admin":
        return True
    admin_email = (os.environ.get("ADMIN_EMAIL") or "").strip().lower()
    return bool(admin_email and (ident.get("email") or "").strip().lower() == admin_email)


def assert_real_tenant(tenant_id: str) -> None:
    """Guard: a tenant id assigned to live data must never be a reserved sentinel."""
    if tenant_id == UNAUTHENTICATED_TENANT:
        raise ValueError("refusing to assign the unauthenticated sentinel as a tenant")


# --- Supabase JWT verification (JWKS / asymmetric) -----------------------
#
# We verify Supabase access tokens against the project's JWKS endpoint (RS256/
# ES256) rather than the HS256 shared secret — asymmetric verification needs no
# server-held secret and rotates cleanly. PyJWKClient caches signing keys in
# memory, so we keep a per-JWKS-URL singleton and do NOT refetch per request.
#
# Fail-closed: any verification or key-fetch failure returns None (the caller
# treats that as "not a Supabase token"), and the request is 401'd downstream —
# a transient JWKS outage fails that one request, it never 500s the server.

_jwks_clients: dict = {}


def _jwks_client(jwks_url: str):
    client = _jwks_clients.get(jwks_url)
    if client is None:
        from jwt import PyJWKClient
        # lifespan=... keeps fetched keys cached; PyJWKClient caches by default.
        client = PyJWKClient(jwks_url, cache_keys=True)
        _jwks_clients[jwks_url] = client
    return client


def verify_supabase_identity(token: str, supabase_url: Optional[str]) -> Optional[dict]:
    """Verify a Supabase access token and return a small identity dict, or None.

    Returns {sub, email, role, plan, app_metadata} on success. role/plan are read
    from the token's app_metadata (set server-side via the admin API, so they are
    tamper-proof — they ride inside the signed JWT). Never raises; fails closed.

    Validates signature (via JWKS), expiry, issuer (`<supabase_url>/auth/v1`),
    and audience (`authenticated`), with clock-skew leeway.
    """
    if not token or not supabase_url:
        return None
    base = supabase_url.rstrip("/")
    jwks_url = f"{base}/auth/v1/.well-known/jwks.json"
    issuer = f"{base}/auth/v1"
    try:
        import jwt
        signing_key = _jwks_client(jwks_url).get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256", "ES256"],
            audience="authenticated",
            issuer=issuer,
            # leeway tolerates client/Supabase clock skew — without it a freshly
            # issued token whose iat is a second ahead of our clock is wrongly
            # rejected (ImmatureSignatureError), which we hit with a real token.
            leeway=60,
            options={"require": ["exp", "sub"]},
        )
        sub = claims.get("sub")
        if not sub:
            return None
        app_meta = claims.get("app_metadata") or {}
        return {
            "sub": sub,
            "email": claims.get("email"),
            "role": app_meta.get("role"),
            "plan": app_meta.get("plan"),
            "app_metadata": app_meta,
        }
    except Exception:
        # Verification failed, key unfetchable, malformed token, expired, wrong
        # aud/iss, HS256-only project (no JWKS) — all fail closed to None.
        return None


def verify_supabase_jwt(token: str, supabase_url: Optional[str]) -> Optional[str]:
    """Return the token's `sub` (tenant id) if valid, else None. Thin wrapper
    over verify_supabase_identity for callers that only need the tenant id."""
    ident = verify_supabase_identity(token, supabase_url)
    return ident["sub"] if ident else None
