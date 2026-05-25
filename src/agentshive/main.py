import os

import uvicorn
from fastmcp import FastMCP
from starlette.responses import JSONResponse
from starlette.routing import Route

from .auth import BearerAuthMiddleware, ProjectContextMiddleware
from .config import load_settings
from .dashboard import register_routes as register_dashboard_routes
from .db import init_engine
from .oauth import AgentsHiveOAuthProvider
from .tools import register_tools


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
        legacy_api_key=settings.api_key,
    )

    mcp = FastMCP(
        name="AgentsHive",
        instructions=(
            "AgentsHive is the bridge between AI Planners (Claude/Codex desktop & mobile) "
            "and AI Coders (Claude Code, Codex CLI). Planners create missions and answer the "
            "Coder's questions. Coders fetch missions, ask the Planner instead of the human, "
            "and submit progress summaries. There is one active mission at a time."
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

    # Middleware order (Starlette wraps in LIFO, so add the innermost LAST):
    #   1. ProjectContextMiddleware (added first → wraps outermost → runs FIRST
    #      on every request, so the ContextVar is populated before BearerAuth or
    #      any downstream handler reads it).
    #   2. BearerAuthMiddleware (added second → wraps innermost → runs SECOND,
    #      gates access to non-public paths with the legacy shared key).
    app.add_middleware(BearerAuthMiddleware, api_key=settings.api_key)
    app.add_middleware(ProjectContextMiddleware)
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
