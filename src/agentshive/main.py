import uvicorn
from fastmcp import FastMCP
from starlette.responses import JSONResponse
from starlette.routing import Route

from .auth import BearerAuthMiddleware
from .config import load_settings
from .dashboard import register_routes as register_dashboard_routes
from .db import init_engine
from .tools import register_tools


def build_app():
    settings = load_settings()
    init_engine(settings)

    mcp = FastMCP(
        name="AgentsHive",
        instructions=(
            "AgentsHive is the bridge between AI Planners (Claude/Codex desktop & mobile) "
            "and AI Coders (Claude Code, Codex CLI). Planners create missions and answer the "
            "Coder's questions. Coders fetch missions, ask the Planner instead of the human, "
            "and submit progress summaries. There is one active mission at a time."
        ),
    )
    register_tools(mcp, settings)

    app = mcp.http_app(path="/mcp")

    async def health(_request):
        return JSONResponse({"status": "ok", "service": "agentshive"})

    app.router.routes.append(Route("/", health))
    app.router.routes.append(Route("/healthz", health))

    # Dashboard routes (v1.4). Registered BEFORE BearerAuthMiddleware so the
    # PUBLIC_PATHS list can let them through to dashboard._require_dashboard_auth,
    # which handles cookie-or-bearer auth separately. The dashboard needs the
    # current MCP tool name list for tools_catalog_hash — capture it now, after
    # register_tools has run.
    register_dashboard_routes(app, settings, _capture_tool_names(mcp))

    app.add_middleware(BearerAuthMiddleware, api_key=settings.api_key)
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
