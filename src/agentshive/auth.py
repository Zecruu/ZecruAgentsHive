from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse


class BearerAuthMiddleware(BaseHTTPMiddleware):
    """Reject any request whose Authorization header doesn't carry the shared API key.

    The Planner connector and the Coder MCP client both present the same bearer token
    in v1. Health-check paths are allowed through unauthenticated so Railway can probe.
    """

    PUBLIC_PATHS = {"/", "/healthz"}

    def __init__(self, app, api_key: str):
        super().__init__(app)
        self._api_key = api_key

    async def dispatch(self, request: Request, call_next):
        if request.url.path in self.PUBLIC_PATHS:
            return await call_next(request)
        header = request.headers.get("authorization", "")
        if not header.lower().startswith("bearer "):
            return JSONResponse({"error": "missing bearer token"}, status_code=401)
        token = header[7:].strip()
        if token != self._api_key:
            return JSONResponse({"error": "invalid bearer token"}, status_code=401)
        return await call_next(request)
