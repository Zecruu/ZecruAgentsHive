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

import asyncio
import hmac
import importlib.resources
import json
import time
from datetime import datetime, timezone
from typing import Any

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlmodel import Session, desc, select
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from starlette.routing import Route

from . import dashboard_events

from .config import Settings
from .db import Message, Mission, Question, Summary, get_engine
from .tools import (
    AGENTSHIVE_VERSION,
    MAX_TEXT_LEN,
    SERVER_STARTED_AT,
    _active_mission,
    _compute_tools_catalog_hash,
    _do_ack_message,
    _do_answer_question,
    _do_mark_mission_done,
    _do_respond_to_summary,
    _do_send_to_coder,
    _message_dict,
    _mission_dict,
    _question_dict,
    _summary_dict,
    _validate_text,
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


def _require_same_origin(request: Request) -> bool:
    """CSRF defense — verify the request's Origin (or Referer) matches our host.

    Belt-to-suspenders on top of the SameSite=Lax cookie. Browsers always send
    Origin on POST requests, so:
      - Origin present and matches our host  → allow (legitimate browser call)
      - Origin present and mismatches        → 403 (cross-origin attack)
      - Origin absent but Referer matches    → allow (older browser fallback)
      - Both absent                          → allow (non-browser caller like curl/cli
                                               with bearer auth — they don't send Origin)
    """
    our_host = (request.headers.get("host") or request.url.netloc or "").lower()
    if not our_host:
        return True  # we can't compare without knowing our own host; let auth gate

    def _host_of(url: str) -> str:
        # Cheap parse — avoid pulling in urllib for one-liner usage. Strip scheme,
        # take up to first '/' or '?'. Lowercase.
        if "://" in url:
            url = url.split("://", 1)[1]
        for sep in ("/", "?", "#"):
            if sep in url:
                url = url.split(sep, 1)[0]
        return url.lower()

    origin = request.headers.get("origin", "").strip()
    if origin:
        return _host_of(origin) == our_host
    referer = request.headers.get("referer", "").strip()
    if referer:
        return _host_of(referer) == our_host
    return True  # no Origin AND no Referer = non-browser caller


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


# ---------- v1.5 write endpoints ----------
# Each wraps a _do_<name> function from tools.py — single source of truth between
# the MCP tool surface and this HTTP surface. The handlers do their own auth +
# CSRF check + input parsing, then delegate.
#
# Status code policy:
#   401 — no/bad auth
#   403 — same-origin CSRF check failed
#   400 — malformed JSON, missing required field, or input failed length validation
#         (validation errors must be 400 so the UI knows to block submission)
#   200 ok=True  — operation succeeded
#   200 ok=False — business-state error (already answered, no active mission, etc.) —
#                  these are valid responses, just not the happy path; UI displays inline
#                  rather than treating as a hard failure


def _is_validation_error(err_dict: dict) -> bool:
    """True if a _do_<name> error came from _validate_text (vs. business-state)."""
    msg = err_dict.get("error", "")
    return ("must be a non-empty string" in msg) or ("exceeds maximum length" in msg)


async def _read_json_body(request: Request):
    """Return (body_dict, error_response) — exactly one is None."""
    try:
        body = await request.json()
    except Exception:
        return None, JSONResponse({"error": "malformed JSON body"}, status_code=400)
    if not isinstance(body, dict):
        return None, JSONResponse({"error": "request body must be a JSON object"}, status_code=400)
    return body, None


def _make_write_handler(settings: Settings, action):
    """Wrap an _do_<name> action with the common auth+CSRF+JSON+delegate pattern.

    `action(body: dict) -> (return_key: str, result_dict)` — the handler does the
    rest. `return_key` is the dict key the caller wants on success
    (e.g., "question" / "message" / "mission").
    """
    async def handler(request: Request) -> Response:
        if not _require_dashboard_auth(request, settings):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        if not _require_same_origin(request):
            return JSONResponse({"error": "cross-origin request rejected"}, status_code=403)
        body, err_resp = await _read_json_body(request)
        if err_resp is not None:
            return err_resp
        try:
            return_key, result = action(body)
        except _BadRequest as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        if "error" in result:
            status = 400 if _is_validation_error(result) else 200
            payload = {"ok": False, **result}
            return JSONResponse(payload, status_code=status)
        return JSONResponse({"ok": True, return_key: result}, status_code=200)
    return handler


class _BadRequest(Exception):
    """Raised inside an `action` callable when the request body is missing required fields."""


def _require_str(body: dict, key: str) -> str:
    val = body.get(key)
    if not isinstance(val, str) or not val:
        raise _BadRequest(f"missing or empty required field '{key}'")
    return val


def _make_answer_handler(settings):
    def action(body):
        question_id = _require_str(body, "question_id")
        answer = body.get("answer", "")
        return "question", _do_answer_question(question_id, answer)
    return _make_write_handler(settings, action)


def _make_respond_handler(settings):
    def action(body):
        summary_id = _require_str(body, "summary_id")
        response = body.get("response", "")
        return "summary", _do_respond_to_summary(summary_id, response)
    return _make_write_handler(settings, action)


def _make_ack_handler(settings):
    def action(body):
        message_id = _require_str(body, "message_id")
        return "message", _do_ack_message(message_id)
    return _make_write_handler(settings, action)


def _make_send_handler(settings):
    def action(body):
        msg_body = body.get("body", "")
        return "message", _do_send_to_coder(msg_body)
    return _make_write_handler(settings, action)


def _make_events_handler(settings: Settings):
    """SSE push channel — clients GET this once, the server holds the connection
    open and ships a state envelope on every state change (and a keepalive comment
    every 15s to prevent proxy idle-timeout).
    """
    KEEPALIVE_SECONDS = 15

    async def events(request: Request) -> Response:
        if not _require_dashboard_auth(request, settings):
            return JSONResponse({"error": "unauthorized"}, status_code=401)

        # Capture the uvicorn event loop lazily on first SSE connect, so sync
        # write paths can schedule queue puts onto it. Starlette 1.x dropped
        # the on_startup hook in favor of lifespan; rather than wiring lifespan
        # just for this, we grab the running loop here. The first subscriber
        # primes it; broadcasts triggered before any subscriber connects
        # silently no-op (no subscribers to wake anyway).
        if dashboard_events._event_loop is None:
            dashboard_events.register_loop(asyncio.get_running_loop())

        sub_id, queue = dashboard_events.subscribe()

        async def event_stream():
            try:
                # Initial state event — connected clients get the current snapshot
                # immediately without waiting for the next mutation. Suggest a 3s
                # reconnect backoff to the browser.
                initial = _build_state_payload(settings)
                yield f"retry: 3000\nevent: state\ndata: {json.dumps(initial)}\n\n".encode("utf-8")

                while True:
                    try:
                        # Wait for either a broadcast sentinel or the keepalive timeout.
                        await asyncio.wait_for(queue.get(), timeout=KEEPALIVE_SECONDS)
                        # Sentinel arrived — push current state.
                        payload = _build_state_payload(settings)
                        yield f"event: state\ndata: {json.dumps(payload)}\n\n".encode("utf-8")
                    except asyncio.TimeoutError:
                        # No event in KEEPALIVE_SECONDS — send an SSE comment to
                        # keep the connection alive through proxy idle timeouts.
                        yield b": keepalive\n\n"
                    except (asyncio.CancelledError, GeneratorExit):
                        # Client disconnected or server is shutting down.
                        break
            finally:
                dashboard_events.unsubscribe(sub_id)

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={
                # Disable proxy buffering for SSE — without this, some intermediate
                # proxies (Railway included sometimes) hold bytes until the connection
                # closes, defeating the whole point of a push channel.
                "Cache-Control": "no-cache, no-transform",
                "X-Accel-Buffering": "no",  # nginx-family hint
                "Connection": "keep-alive",
            },
        )

    return events


def _make_mark_done_handler(settings: Settings):
    async def handler(request: Request) -> Response:
        if not _require_dashboard_auth(request, settings):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        if not _require_same_origin(request):
            return JSONResponse({"error": "cross-origin request rejected"}, status_code=403)
        # mark-done takes no body — but still tolerate {} JSON if sent
        try:
            await request.body()
        except Exception:
            pass
        result = _do_mark_mission_done()
        if "error" in result:
            return JSONResponse({"ok": False, **result}, status_code=200)
        return JSONResponse({"ok": True, "mission": result}, status_code=200)
    return handler


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
    # v1.5 write endpoints
    app.router.routes.append(Route("/api/dashboard/answer", _make_answer_handler(settings), methods=["POST"]))
    app.router.routes.append(Route("/api/dashboard/respond", _make_respond_handler(settings), methods=["POST"]))
    app.router.routes.append(Route("/api/dashboard/ack", _make_ack_handler(settings), methods=["POST"]))
    app.router.routes.append(Route("/api/dashboard/send", _make_send_handler(settings), methods=["POST"]))
    app.router.routes.append(Route("/api/dashboard/mark-done", _make_mark_done_handler(settings), methods=["POST"]))
    # v1.6 SSE push channel
    app.router.routes.append(Route("/api/dashboard/events", _make_events_handler(settings), methods=["GET"]))
