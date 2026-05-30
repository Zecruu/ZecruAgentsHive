import os
from pathlib import Path

import uvicorn
from fastmcp import FastMCP
from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from .admin import register_admin_routes
from .auth import BearerAuthMiddleware, ProjectContextMiddleware, TenantContextMiddleware, WebCorsMiddleware
from .config import load_settings
from .dashboard import register_routes as register_dashboard_routes
from .db import init_engine
from .oauth import AgentsHiveOAuthProvider
from .tools import register_tools
from .web import register_web_routes


class _SPAStaticFiles(StaticFiles):
    """StaticFiles that falls back to index.html on a 404 (SPA client routing).

    Serves real assets normally; any unknown path under the mount returns the
    app shell so client-side routes resolve. Safe for our same-origin webapp.
    """

    async def get_response(self, path, scope):
        try:
            return await super().get_response(path, scope)
        except HTTPException as exc:
            if exc.status_code == 404:
                return await super().get_response("index.html", scope)
            raise


def _webapp_dist_dir() -> Path:
    """Where the built companion webapp (apps/web) lives on the deployed FS.

    Overridable via AGENTSHIVE_WEBAPP_DIST; defaults to <repo-root>/apps/web/dist
    (this file is src/agentshive/main.py, so parents[2] is the repo root). The
    Railway build is responsible for producing dist/ there.
    """
    override = os.environ.get("AGENTSHIVE_WEBAPP_DIST")
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[2] / "apps" / "web" / "dist"


def build_app():
    settings = load_settings()
    init_engine(settings)

    # v1.7: the OAuth provider needs to know our PUBLIC base URL so it can build
    # accurate AS metadata, issuer claim, and canonical resource for audience
    # validation. Default to localhost for dev; Railway should set
    # AGENTSHIVE_BASE_URL=https://<service>.up.railway.app at runtime.
    base_url = os.environ.get("AGENTSHIVE_BASE_URL", f"http://localhost:{settings.port}").rstrip("/")
    oauth_provider = AgentsHiveOAuthProvider(
        base_url=base_url,
        mcp_mount_path="/mcp",
        # Q2 (KEEP LEGACY KEY FOREVER): hand the provider our shared API key so
        # /mcp callers using the v1.0-v1.6 bearer auth keep working.
        legacy_api_key=settings.api_key if settings.legacy_key_enabled else None,
        # v2.x: accept JWKS-verified Supabase access tokens on /mcp too.
        supabase_url=settings.supabase_url,
    )

    mcp = FastMCP(
        name="AgentsHive",
        instructions=(
            "AgentsHive is the bridge between AI Planners (Claude/Codex desktop & mobile) "
            "and AI Coders (Claude Code, Codex CLI). Planners create missions and answer the "
            "Coder's questions. Coders fetch missions, ask the Planner instead of the human, "
            "and submit progress summaries. There is one active mission at a time. "
            # Mission A nudge — guidance, not enforcement. Cuts the "fire send_to_coder into a void" failure mode.
            "MUTUAL STATE AWARENESS: Before send_to_coder or answer_question, you SHOULD call "
            "list_agent_states() to see if the target coder is alive. If state in (stale, dead) "
            "for the target, prefer notifying the user via send_to_user instead of firing the "
            "message into a void. Before any long wait (deploy, install, tag), call "
            "set_my_state('working', detail, expected_seconds=N) so the operator + peers can "
            "see what you're doing; call set_my_state('idle') when the work lands. Coders MUST "
            "pass their normalized coder_id as agent_key; the planner defaults to 'planner'."
        ),
        auth=oauth_provider,
    )
    register_tools(mcp, settings)

    app = mcp.http_app(path="/mcp")

    async def health(_request):
        return JSONResponse({"status": "ok", "service": "agentshive"})

    app.router.routes.append(Route("/", health))
    app.router.routes.append(Route("/healthz", health))

    # Dashboard routes (v1.4) + v1.7 OAuth consent. Registered BEFORE
    # BearerAuthMiddleware so the PUBLIC_PATHS list can let them through to
    # the per-route auth checks (cookie-or-bearer for the dashboard,
    # API-key-or-cookie-via-form for /oauth/consent).
    register_dashboard_routes(app, settings, _capture_tool_names(mcp), oauth_provider=oauth_provider)

    # v2.x admin/superuser router (/admin/*). Each handler enforces is_admin();
    # the paths are in BearerAuthMiddleware.PUBLIC_PATHS so the admin's Supabase
    # token (not the legacy shared key) reaches the per-handler gate.
    register_admin_routes(app, settings)

    # v2.x companion-webapp router (/web/*). Supabase-JWT, tenant-scoped; every
    # handler wrapped in _web_guard. In PUBLIC_PATHS so the user's/desktop's
    # Supabase token reaches the per-handler gate (not the legacy shared key).
    register_web_routes(app, settings)

    # v2.x: serve the built companion webapp (apps/web) as static files at /app
    # (same origin → its /web/* data calls work with no CORS). Mounted only when
    # the build is present, so a server without a built webapp (local/dev) is
    # unaffected. /app is exempted in BearerAuthMiddleware (static is public; the
    # webapp's data calls hit the already-public, JWT-guarded /web/* router). The
    # /app prefix can't shadow /api, /web, /admin, /mcp, /oauth, or /dashboard.
    dist_dir = _webapp_dist_dir()
    if dist_dir.is_dir():
        app.router.routes.append(
            Mount("/app", app=_SPAStaticFiles(directory=str(dist_dir), html=True), name="webapp")
        )

    # Middleware order (Starlette wraps in LIFO, so add the innermost LAST):
    #   1. ProjectContextMiddleware + TenantContextMiddleware (added last → wrap
    #      outermost → run FIRST on every request, so the project + tenant
    #      ContextVars are populated before BearerAuth or any downstream handler
    #      / MCP tool reads them).
    #   2. BearerAuthMiddleware (added first → wraps innermost → runs after the
    #      context middlewares, gates non-public paths with the legacy shared key).
    app.add_middleware(BearerAuthMiddleware, api_key=settings.api_key, legacy_key_enabled=settings.legacy_key_enabled)
    app.add_middleware(ProjectContextMiddleware)
    app.add_middleware(TenantContextMiddleware, api_key=settings.api_key, supabase_url=settings.supabase_url, legacy_key_enabled=settings.legacy_key_enabled)
    # Outermost: CORS for /web/* (answers OPTIONS preflight before auth runs).
    app.add_middleware(WebCorsMiddleware)
    return app, settings


def _capture_tool_names(mcp) -> list[str]:
    """Synchronously fetch the registered tool name list for dashboard hash computation.

    mcp.list_tools() is async; we're called from sync build_app() before uvicorn's
    event loop exists. Use asyncio.run on a fresh loop just for this one call.
    """
    import asyncio
    tools = asyncio.run(mcp.list_tools())
    if isinstance(tools, list):
        return [t.name for t in tools]
    return list(tools.keys())


def main():
    app, settings = build_app()
    uvicorn.run(app, host="0.0.0.0", port=settings.port, log_level="info")


if __name__ == "__main__":
    main()
