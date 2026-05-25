import hmac

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse


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
        # v1.5 write endpoints — dashboard handlers enforce cookie-or-bearer auth
        # themselves; global bearer-only middleware would reject browser POSTs (which
        # carry the session cookie, not Authorization).
        "/api/dashboard/answer",
        "/api/dashboard/respond",
        "/api/dashboard/ack",
        "/api/dashboard/send",
        "/api/dashboard/mark-done",
        # v1.6 SSE push channel — dashboard handler enforces cookie-or-bearer auth.
        "/api/dashboard/events",
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

    def __init__(self, app, api_key: str):
        super().__init__(app)
        self._api_key = api_key

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if path in self.PUBLIC_PATHS or path.startswith("/.well-known/oauth-"):
            return await call_next(request)
        header = request.headers.get("authorization", "")
        if not header.lower().startswith("bearer "):
            return JSONResponse({"error": "missing bearer token"}, status_code=401)
        token = header[7:].strip()
        if not hmac.compare_digest(token.encode("utf-8"), self._api_key.encode("utf-8")):
            return JSONResponse({"error": "invalid bearer token"}, status_code=401)
        return await call_next(request)
