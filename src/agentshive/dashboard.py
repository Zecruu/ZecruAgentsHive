"""Read-only web dashboard for AgentsHive (v1.4).

Routes mounted on the same Starlette app as /mcp:
    GET  /dashboard/login        — HTML login form
    POST /dashboard/login        — validate AGENTSHIVE_API_KEY, issue signed cookie
    POST /dashboard/logout       — clear cookie + redirect to login
    GET  /dashboard              — single-page app (auth required)
    GET  /api/dashboard/state    — unified state JSON (auth required)

Auth: signed cookie (12h max-age) OR Authorization: Bearer header. The same shared
AGENTSHIVE_API_KEY backs both — cookie signing key is derived from it via
itsdangerous, so rotating the env var also invalidates every existing session.
"""

import hmac
import importlib.resources
import time
from datetime import datetime, timezone
from typing import Any

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlmodel import Session, desc, select
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from .config import Settings
from .db import Message, Mission, Question, Summary, get_engine
from .tools import (
    AGENTSHIVE_VERSION,
    SERVER_STARTED_AT,
    _active_mission,
    _compute_tools_catalog_hash,
    _message_dict,
    _mission_dict,
    _question_dict,
    _summary_dict,
)


COOKIE_NAME = "agentshive_dash_session"
COOKIE_MAX_AGE_SECONDS = 12 * 60 * 60  # 12 hours
SPEC_PREVIEW_CHARS = 300


def _serializer(settings: Settings) -> URLSafeTimedSerializer:
    # secret = the shared API key. Rotating AGENTSHIVE_API_KEY auto-invalidates
    # all existing dashboard sessions (signatures fail to verify).
    return URLSafeTimedSerializer(secret_key=settings.api_key, salt="agentshive.dashboard.v1")


def _is_request_secure(request: Request) -> bool:
    """True if the request reached the *edge* over HTTPS, even if we received it as HTTP.

    Railway and most reverse proxies terminate TLS at the edge and forward plain HTTP
    to the app, setting X-Forwarded-Proto=https so the app can still distinguish.
    Falls back to request.url.scheme when no proxy header is present (local dev).
    """
    forwarded = request.headers.get("x-forwarded-proto", "").strip().split(",")[0].strip()
    if forwarded:
        return forwarded.lower() == "https"
    return request.url.scheme == "https"


def _cookie_kwargs(request: Request) -> dict[str, Any]:
    return {
        "httponly": True,
        "samesite": "lax",
        "secure": _is_request_secure(request),
        "max_age": COOKIE_MAX_AGE_SECONDS,
        "path": "/",
    }


def _require_dashboard_auth(request: Request, settings: Settings) -> bool:
    """Return True if the request carries a valid bearer header OR a valid signed cookie."""
    # Bearer header path — same key as /mcp, constant-time compare.
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        presented = header[7:].strip()
        if hmac.compare_digest(presented.encode("utf-8"), settings.api_key.encode("utf-8")):
            return True

    # Cookie path.
    raw = request.cookies.get(COOKIE_NAME)
    if not raw:
        return False
    try:
        _serializer(settings).loads(raw, max_age=COOKIE_MAX_AGE_SECONDS)
        return True
    except (BadSignature, SignatureExpired):
        return False


# ---------- HTML templates ----------
# v1.5: moved from inline strings to src/agentshive/templates/{dashboard,login}.html.
# Trigger conditions from v1.4 spec both fired (page > 500 lines AND write actions
# being added). Loaded once at module import via importlib.resources, which works
# identically for wheel-installed and editable installs.

def _load_template(name: str) -> str:
    """Read a static HTML template shipped inside the agentshive.templates package."""
    return (importlib.resources.files("agentshive.templates") / name).read_text(encoding="utf-8")


DASHBOARD_HTML = _load_template("dashboard.html")
LOGIN_HTML = _load_template("login.html")




def _render_login(error: str | None = None) -> str:
    block = f'<div class="error">{error}</div>' if error else ""
    return LOGIN_HTML.replace("{error_block}", block)


# ---------- Route handlers ----------


def _make_login_get(_settings: Settings):
    async def login_get(_request: Request) -> Response:
        return HTMLResponse(_render_login())
    return login_get


def _make_login_post(settings: Settings):
    async def login_post(request: Request) -> Response:
        form = await request.form()
        presented = (form.get("api_key") or "").strip()
        if not presented or not hmac.compare_digest(
            presented.encode("utf-8"), settings.api_key.encode("utf-8")
        ):
            return HTMLResponse(_render_login("Invalid API key."), status_code=200)
        token = _serializer(settings).dumps({"iat": int(time.time())})
        resp = RedirectResponse(url="/dashboard", status_code=302)
        resp.set_cookie(COOKIE_NAME, token, **_cookie_kwargs(request))
        return resp
    return login_post


def _make_logout(_settings: Settings):
    async def logout(_request: Request) -> Response:
        resp = RedirectResponse(url="/dashboard/login", status_code=302)
        resp.delete_cookie(COOKIE_NAME, path="/")
        return resp
    return logout


def _make_dashboard(settings: Settings):
    async def dashboard(request: Request) -> Response:
        if not _require_dashboard_auth(request, settings):
            return RedirectResponse(url="/dashboard/login", status_code=302)
        return HTMLResponse(DASHBOARD_HTML)
    return dashboard


def _build_state_payload(_settings: Settings) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    with Session(get_engine()) as session:
        active = _active_mission(session)
        active_dict: dict[str, Any] | None = None
        if active is not None:
            d = _mission_dict(active)
            d["spec_preview"] = (active.spec or "")[:SPEC_PREVIEW_CHARS]
            active_dict = d

        recent_rows = session.exec(
            select(Mission)
            .where(Mission.status.in_(("done", "superseded")))
            .order_by(desc(Mission.created_at))
            .limit(5)
        ).all()
        recent = [_mission_dict(m) for m in recent_rows]

        pending_q: list[dict[str, Any]] = []
        pending_s: list[dict[str, Any]] = []
        c2p: list[dict[str, Any]] = []
        p2c: list[dict[str, Any]] = []
        if active is not None:
            pending_q = [
                _question_dict(q)
                for q in session.exec(
                    select(Question)
                    .where(Question.mission_id == active.id, Question.answer.is_(None))
                    .order_by(Question.created_at)
                ).all()
            ]
            pending_s = [
                _summary_dict(s)
                for s in session.exec(
                    select(Summary)
                    .where(Summary.mission_id == active.id, Summary.response.is_(None))
                    .order_by(Summary.created_at)
                ).all()
            ]
            c2p = _recent_messages(session, active.id, direction="coder_to_planner")
            p2c = _recent_messages(session, active.id, direction="planner_to_coder")
        else:
            # No active mission: surface the last few messages across recent missions for context.
            c2p = _recent_messages_global(session, direction="coder_to_planner")
            p2c = _recent_messages_global(session, direction="planner_to_coder")

        # Heartbeat from active mission. SQLite reads TIMESTAMP back as naive datetimes
        # even when we wrote them with tzinfo=UTC, so coerce before subtracting against
        # the aware `now`.
        hb_last = active.coder_last_seen if active else None
        if hb_last is not None:
            if hb_last.tzinfo is None:
                hb_last_aware = hb_last.replace(tzinfo=timezone.utc)
            else:
                hb_last_aware = hb_last
            freshness = max(0, int((now - hb_last_aware).total_seconds()))
            heartbeat = {"last_seen": hb_last_aware.isoformat(), "freshness_seconds": freshness}
        else:
            heartbeat = {"last_seen": None, "freshness_seconds": None}

        # Server info — match the get_server_info tool's shape.
        # We compute the catalog hash from the SQLModel-driven tool surface registered at
        # the FastMCP instance; the dashboard doesn't have direct access here, so we fall
        # back to a sentinel ("unknown") if needed. Practically, get_server_info is the
        # authoritative source and the dashboard just mirrors what the tool layer reports.
        # We can recompute here, but we'd need the FastMCP instance. For now, surface the
        # version + started_at; the hash is harder. See follow-up below.
        server_info = {
            "server_version": AGENTSHIVE_VERSION,
            "tools_catalog_hash": _compute_tools_catalog_hash(_DASHBOARD_TOOL_NAMES_CACHE),
            "started_at": SERVER_STARTED_AT.isoformat(),
        }

        return {
            "active_mission": active_dict,
            "recent_missions": recent,
            "pending_questions": pending_q,
            "pending_summaries": pending_s,
            "messages": {"coder_to_planner": c2p, "planner_to_coder": p2c},
            "server_info": server_info,
            "coder_heartbeat": heartbeat,
        }


def _recent_messages(session: Session, mission_id: str, direction: str, total: int = 10) -> list[dict[str, Any]]:
    """Last `total/2` undelivered + last `total/2` delivered, oldest first within each group."""
    half = total // 2
    undelivered = list(reversed(session.exec(
        select(Message)
        .where(
            Message.mission_id == mission_id,
            Message.direction == direction,
            Message.delivered_at.is_(None),
        )
        .order_by(desc(Message.created_at))
        .limit(half)
    ).all()))
    delivered = list(reversed(session.exec(
        select(Message)
        .where(
            Message.mission_id == mission_id,
            Message.direction == direction,
            Message.delivered_at.is_not(None),
        )
        .order_by(desc(Message.created_at))
        .limit(half)
    ).all()))
    return [_message_dict(m) for m in (undelivered + delivered)]


def _recent_messages_global(session: Session, direction: str, total: int = 10) -> list[dict[str, Any]]:
    rows = session.exec(
        select(Message)
        .where(Message.direction == direction)
        .order_by(desc(Message.created_at))
        .limit(total)
    ).all()
    return [_message_dict(m) for m in reversed(rows)]


# Populated at register time so _build_state_payload can compute the catalog hash without
# needing a direct FastMCP reference inside the request handler.
_DASHBOARD_TOOL_NAMES_CACHE: list[str] = []


def _make_state(settings: Settings):
    async def state(request: Request) -> Response:
        if not _require_dashboard_auth(request, settings):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        payload = _build_state_payload(settings)
        return JSONResponse(payload)
    return state


def register_routes(app, settings: Settings, tool_names: list[str]) -> None:
    """Mount dashboard routes onto the given Starlette app.

    tool_names: the registered MCP tool names, captured after register_tools runs in main.py.
                Used for the tools_catalog_hash field in the state payload.
    """
    global _DASHBOARD_TOOL_NAMES_CACHE
    _DASHBOARD_TOOL_NAMES_CACHE = list(tool_names)

    app.router.routes.append(Route("/dashboard/login", _make_login_get(settings), methods=["GET"]))
    app.router.routes.append(Route("/dashboard/login", _make_login_post(settings), methods=["POST"]))
    app.router.routes.append(Route("/dashboard/logout", _make_logout(settings), methods=["POST"]))
    app.router.routes.append(Route("/dashboard", _make_dashboard(settings), methods=["GET"]))
    app.router.routes.append(Route("/api/dashboard/state", _make_state(settings), methods=["GET"]))
