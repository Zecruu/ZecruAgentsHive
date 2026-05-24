import uvicorn
from fastmcp import FastMCP
from starlette.responses import JSONResponse
from starlette.routing import Route

from .auth import BearerAuthMiddleware
from .config import load_settings
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

    app.add_middleware(BearerAuthMiddleware, api_key=settings.api_key)
    return app, settings


def main():
    app, settings = build_app()
    uvicorn.run(app, host="0.0.0.0", port=settings.port, log_level="info")


if __name__ == "__main__":
    main()
