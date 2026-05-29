import hmac

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response


class WebCorsMiddleware(BaseHTTPMiddleware):
    """v2.x: CORS for the companion-webapp surface (/web/*) so a browser on a
    different origin (the user's phone) can call it. The /web API authenticates
    via the Supabase JWT bearer (NOT cookies), so a permissive Allow-Origin is
    safe — there are no credentials to leak. Answers OPTIONS preflight directly
    (before auth) since preflight carries no Authorization header.
    """

    _HEADERS = {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
        "Access-Control-Allow-Headers": "Authorization, Content-Type, apikey",
        "Access-Control-Max-Age": "600",
    }

    async def dispatch(self, request: Request, call_next):
        if not request.url.path.startswith("/web/"):
            return await call_next(request)
        if request.method == "OPTIONS":
            return Response(status_code=204, headers=dict(self._HEADERS))
        resp = await call_next(request)
        for k, v in self._HEADERS.items():
            resp.headers[k] = v
        return resp

from .project import (
    DEFAULT_PROJECT_SLUG,
    PROJECT_CONTEXT,
    SLUG_PATTERN,
)
from .tenant import (
    IDENTITY_CONTEXT,
    LEGACY_TENANT,
    TENANT_CONTEXT,
    UNAUTHENTICATED_TENANT,
    verify_supabase_identity,
)


class TenantContextMiddleware(BaseHTTPMiddleware):
    """v2.x: read the Authorization bearer on every request and set TENANT_CONTEXT
    BEFORE downstream middleware / routes / MCP tools run — the same pattern as
    ProjectContextMiddleware.

    Tenant assignment is coupled to WHICH auth method the bearer satisfies:
      - bearer == legacy shared key   → tenant = LEGACY_TENANT
      - bearer is a valid Supabase JWT → tenant = the token's `sub`
      - bearer is a known OAuth access token (Claude-app connector) → its stored tenant
      - bearer present but none of the above → UNAUTHENTICATED_TENANT (never matches
        a real project; the request is 401'd upstream by BearerAuth/the SDK anyway)
      - NO bearer (cookie-authed dashboard / public health / in-process) → LEGACY_TENANT

    It never rejects — gating stays with BearerAuthMiddleware and the SDK's bearer
    backend. This only resolves identity so tools can scope by tenant.
    """

    def __init__(self, app, api_key: str, supabase_url=None, legacy_key_enabled: bool = True):
        super().__init__(app)
        self._api_key = api_key
        self._supabase_url = supabase_url
        self._legacy_key_enabled = legacy_key_enabled

    async def dispatch(self, request: Request, call_next):
        tenant = LEGACY_TENANT
        identity = None
        header = request.headers.get("authorization", "")
        if header.lower().startswith("bearer "):
            token = header[7:].strip()
            if self._legacy_key_enabled and self._api_key and hmac.compare_digest(
                token.encode("utf-8"), self._api_key.encode("utf-8")
            ):
                tenant = LEGACY_TENANT
            else:
                ident = verify_supabase_identity(token, self._supabase_url)
                if ident:
                    tenant = ident["sub"]
                    identity = ident
                else:
                    # Maybe a valid OAuth access token (Claude-app connector).
                    from .db import tenant_for_oauth_token
                    oauth_tenant = tenant_for_oauth_token(token)
                    tenant = oauth_tenant or UNAUTHENTICATED_TENANT
        # Ban enforcement: a banned real tenant is made inert immediately (its
        # token resolves to no project and is_admin() is false). The legacy and
        # sentinel tenants are never "banned".
        if tenant not in (LEGACY_TENANT, UNAUTHENTICATED_TENANT):
            from .db import is_tenant_banned
            if is_tenant_banned(tenant):
                tenant = UNAUTHENTICATED_TENANT
                identity = None
        ctx_token = TENANT_CONTEXT.set(tenant)
        id_token = IDENTITY_CONTEXT.set(identity)
        try:
            return await call_next(request)
        finally:
            TENANT_CONTEXT.reset(ctx_token)
            IDENTITY_CONTEXT.reset(id_token)


class ProjectContextMiddleware(BaseHTTPMiddleware):
    """v1.9: read `?project=<slug>` from every incoming request and set the
    PROJECT_CONTEXT ContextVar before downstream middleware / routes run.

    Falls back to `default` when:
      - no `?project=` is supplied (legacy callers, pre-v1.9 clients)
      - the supplied slug fails the format regex (malformed input is silently
        coerced to default rather than 400'd — this is safer for the OAuth +
        well-known surface, which never carries a project anyway, and it
        matches Q1's "default is reserved for legacy/unscoped" semantics)

    The ContextVar token is reset in finally so concurrent requests don't
    leak each other's project context. Since we register this middleware
    in main.py BEFORE BearerAuthMiddleware, every downstream handler — MCP
    transport, dashboard routes, OAuth endpoints — sees a populated
    PROJECT_CONTEXT.
    """

    async def dispatch(self, request: Request, call_next):
        raw = (request.query_params.get("project") or "").strip().lower()
        slug = raw if (raw and SLUG_PATTERN.fullmatch(raw)) else DEFAULT_PROJECT_SLUG
        token = PROJECT_CONTEXT.set(slug)
        try:
            return await call_next(request)
        finally:
            PROJECT_CONTEXT.reset(token)


class BearerAuthMiddleware(BaseHTTPMiddleware):
    """Reject any request whose Authorization header doesn't carry the shared API key.

    The Planner connector and the Coder MCP client both present the same bearer token
    in v1. Health-check paths are allowed through unauthenticated so Railway can probe.
    """

    # Dashboard routes have their own (cookie-or-bearer) auth check via
    # dashboard._require_dashboard_auth, so the global bearer middleware lets them
    # through. The login/logout pages need to be reachable without prior auth.
    PUBLIC_PATHS = {
        "/", "/healthz",
        # v1.7: /mcp is now protected by the FastMCP-mounted SDK BearerAuthBackend
        # (wired by passing `auth=AgentsHiveOAuthProvider(...)` to FastMCP). That
        # backend validates both OAuth access tokens and the legacy shared key —
        # via AgentsHiveOAuthProvider.load_access_token — so this outer middleware
        # would only double-gate and falsely 401 OAuth callers. Let the SDK do it.
        "/mcp",
        "/dashboard", "/dashboard/login", "/dashboard/logout",
        "/api/dashboard/state",
        # v2.x read-only mission export (desktop writes agentsmissions/ docs);
        # enforces cookie-or-bearer auth inside the handler.
        "/api/dashboard/missions/export",
        # v1.5 write endpoints — dashboard handlers enforce cookie-or-bearer auth
        # themselves; global bearer-only middleware would reject browser POSTs (which
        # carry the session cookie, not Authorization).
        "/api/dashboard/answer",
        "/api/dashboard/respond",
        "/api/dashboard/ack",
        "/api/dashboard/send",
        # v1.8 inbox: user → planner chat post. Per-route auth (cookie or bearer)
        # enforced inside the dashboard handler.
        "/api/dashboard/send-to-planner",
        "/api/dashboard/mark-done",
        # v1.6 SSE push channel — dashboard handler enforces cookie-or-bearer auth.
        "/api/dashboard/events",
        # v1.9 Projects CRUD — dashboard handlers enforce cookie-or-bearer auth
        # themselves. The archive route is path-parameterized; the dispatch
        # loop below uses startswith for the /api/dashboard/projects/ prefix
        # since exact-match wouldn't cover /<slug>/archive.
        "/api/dashboard/projects",
        # v1.7 OAuth surface — these endpoints MUST be reachable without a prior
        # bearer token (it's the user-facing path to GET one). Auth, where needed,
        # is enforced inside the SDK handlers (PKCE, client validation) or the
        # consent handler (API key / cookie). DCR (/register) is rate-limited via
        # the LRU cap in oauth.py rather than via bearer auth, per Planner Q3.
        "/.well-known/oauth-authorization-server",
        # The PRM route is actually mounted at
        # /.well-known/oauth-protected-resource{resource_path} per RFC 9728 §3.1,
        # so the literal here matches our /mcp mount. The prefix exemption in
        # dispatch() also covers any future path beneath /.well-known/oauth-.
        "/.well-known/oauth-protected-resource",
        "/.well-known/oauth-protected-resource/mcp",
        "/authorize",
        "/token",
        "/register",
        "/revoke",
        "/oauth/consent",
    }

    def __init__(self, app, api_key: str, legacy_key_enabled: bool = True):
        super().__init__(app)
        self._api_key = api_key
        self._legacy_key_enabled = legacy_key_enabled

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if (
            path in self.PUBLIC_PATHS
            or path.startswith("/.well-known/oauth-")
            or path.startswith("/api/dashboard/projects/")  # /<slug>/archive
            # v2.x admin router: parameterized paths; each handler enforces
            # is_admin() on the verified Supabase token (the legacy-key-only
            # check here would otherwise 401 the admin's token).
            or path.startswith("/admin/")
            # v2.x companion-webapp router: Supabase-JWT, _web_guard per handler.
            or path.startswith("/web/")
        ):
            return await call_next(request)
        header = request.headers.get("authorization", "")
        if not header.lower().startswith("bearer "):
            return JSONResponse({"error": "missing bearer token"}, status_code=401)
        token = header[7:].strip()
        if not self._legacy_key_enabled or not hmac.compare_digest(token.encode("utf-8"), self._api_key.encode("utf-8")):
            return JSONResponse({"error": "invalid bearer token"}, status_code=401)
        return await call_next(request)
