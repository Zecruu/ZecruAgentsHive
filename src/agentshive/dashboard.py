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


# ---------- HTML page ----------
# Inline string per scope decision (v1.4 spec): one file holds the whole feature.
# Will move to a templates/ file once the page grows past ~300 lines or starts needing
# server-side substitution. For now it's a static SPA — all dynamic content is fetched
# from /api/dashboard/state via JS.

DASHBOARD_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>AgentsHive Dashboard</title>
<style>
:root { color-scheme: dark; }
body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
       background: #0e0f12; color: #d6d9e0; margin: 0; padding: 16px; }
main { max-width: 1100px; margin: 0 auto; }
.card { background: #16181d; border: 1px solid #232732; border-radius: 8px;
        padding: 14px 16px; margin-bottom: 12px; }
.row { display: flex; gap: 12px; align-items: center; flex-wrap: wrap; }
.spread { justify-content: space-between; }
h1 { font-size: 18px; margin: 0 0 4px 0; font-weight: 600; }
h2 { font-size: 14px; margin: 0 0 8px 0; color: #9aa0ab; text-transform: uppercase; letter-spacing: 0.04em; }
.badge { font-size: 11px; padding: 2px 8px; border-radius: 10px; font-weight: 600; }
.badge.active   { background: #1e3a2b; color: #6fdc9f; }
.badge.done     { background: #2a2a3a; color: #8a8fa7; }
.badge.superseded { background: #3a2a2a; color: #d09a8a; }
.badge.none     { background: #2a2a3a; color: #6a6f7a; }
.heartbeat { font-size: 12px; padding: 4px 10px; border-radius: 6px; font-variant-numeric: tabular-nums; }
.heartbeat.fresh  { background: #122a1c; color: #6fdc9f; }
.heartbeat.warm   { background: #2a2418; color: #e8c47a; }
.heartbeat.stale  { background: #2a1818; color: #ff8a78; }
.heartbeat.none   { background: #1a1c22; color: #6a6f7a; }
small.dim { color: #6a6f7a; font-size: 11px; }
.spec-preview { white-space: pre-wrap; color: #b0b6c2; font-size: 13px;
                margin-top: 8px; line-height: 1.45; max-height: 60px; overflow: hidden;
                transition: max-height 0.2s; }
.spec-preview.expanded { max-height: 60vh; overflow: auto; }
.toggle { font-size: 12px; color: #7aa9ff; cursor: pointer; user-select: none; margin-top: 4px; display: inline-block; }
.panel-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
.panel-grid > div { background: #16181d; border: 1px solid #232732; border-radius: 8px; padding: 14px 16px; }
.item { border-top: 1px solid #232732; padding: 10px 0; font-size: 13px; }
.item:first-child { border-top: none; }
.item .body { white-space: pre-wrap; word-wrap: break-word; color: #d6d9e0; }
.item .meta { color: #6a6f7a; font-size: 11px; margin-top: 4px; display: flex; gap: 8px; flex-wrap: wrap; }
.empty { color: #6a6f7a; font-size: 13px; padding: 6px 0; }
.badge.unacked { background: #2a2418; color: #e8c47a; }
.badge.acked   { background: #1a2a1c; color: #6fdc9f; }
.badge.redeliv { background: #3a2a2a; color: #d09a8a; }
.banner { position: fixed; top: 0; left: 0; right: 0; background: #ff8a78; color: #200; padding: 8px;
          text-align: center; font-weight: 600; z-index: 10; display: none; }
.banner.show { display: block; }
button { background: #232732; color: #d6d9e0; border: 1px solid #2c303a;
         padding: 5px 12px; border-radius: 6px; font-size: 12px; cursor: pointer; }
button:hover { background: #2c303a; }
form { margin: 0; }
.logout-form { display: inline; }
</style>
</head>
<body>
<div id="connlost" class="banner">Lost connection — retrying…</div>
<main>
  <div class="card" id="header">
    <div class="row spread">
      <div>
        <div class="row" style="gap:8px;">
          <h1 id="mission-name">…</h1>
          <span id="mission-status" class="badge none">…</span>
        </div>
        <small class="dim" id="mission-meta">loading…</small>
      </div>
      <div class="row">
        <span id="heartbeat" class="heartbeat none">…</span>
        <form class="logout-form" method="post" action="/dashboard/logout">
          <button type="submit">Logout</button>
        </form>
      </div>
    </div>
    <div id="spec-preview" class="spec-preview"></div>
    <span class="toggle" id="spec-toggle" style="display:none;">Show full spec</span>
    <div class="row" style="margin-top:8px;">
      <small class="dim" id="server-info">server: …</small>
    </div>
  </div>

  <div class="panel-grid">
    <div>
      <h2>Pending Questions</h2>
      <div id="questions"><div class="empty">Loading…</div></div>
    </div>
    <div>
      <h2>Pending Summaries</h2>
      <div id="summaries"><div class="empty">Loading…</div></div>
    </div>
  </div>

  <div class="card">
    <h2>Messages</h2>
    <div class="panel-grid">
      <div>
        <h2 style="color:#7aa9ff;">Coder → Planner</h2>
        <div id="msgs-c2p"><div class="empty">Loading…</div></div>
      </div>
      <div>
        <h2 style="color:#7aa9ff;">Planner → Coder</h2>
        <div id="msgs-p2c"><div class="empty">Loading…</div></div>
      </div>
    </div>
  </div>
</main>

<script>
const POLL_MS = 3000;
const $ = (id) => document.getElementById(id);
let specExpanded = false;
let fullSpec = "";

function ago(iso) {
  if (!iso) return "";
  const sec = Math.max(0, Math.floor((Date.now() - new Date(iso).getTime()) / 1000));
  if (sec < 60)  return sec + "s ago";
  if (sec < 3600) return Math.floor(sec/60) + "m ago";
  if (sec < 86400) return Math.floor(sec/3600) + "h ago";
  return Math.floor(sec/86400) + "d ago";
}

function escapeHtml(s) {
  return String(s || "").replace(/[&<>"']/g, c => ({
    "&":"&amp;","<":"&lt;",">":"&gt;","\\"":"&quot;","'":"&#39;"
  }[c]));
}

function renderItem(body, metas) {
  return `<div class="item"><div class="body">${escapeHtml(body)}</div>` +
         `<div class="meta">${metas.join("")}</div></div>`;
}

function renderQuestions(qs) {
  if (!qs.length) return `<div class="empty">No pending questions.</div>`;
  return qs.map(q => renderItem(q.body, [
    `<span>${ago(q.created_at)}</span>`,
    `<small class="dim">id ${q.question_id.slice(0,8)}</small>`,
  ])).join("");
}

function renderSummaries(ss) {
  if (!ss.length) return `<div class="empty">No pending summaries.</div>`;
  return ss.map(s => renderItem(s.body, [
    `<span>${ago(s.created_at)}</span>`,
    `<small class="dim">id ${s.summary_id.slice(0,8)}</small>`,
  ])).join("");
}

function renderMessages(ms) {
  if (!ms.length) return `<div class="empty">No messages.</div>`;
  return ms.map(m => {
    const ackBadge = m.delivered_at
      ? `<span class="badge acked">acked</span>`
      : `<span class="badge unacked">unacked</span>`;
    const redeliv = (m.redelivery_count || 0) > 0
      ? `<span class="badge redeliv">redelivered ×${m.redelivery_count}</span>`
      : "";
    return renderItem(m.body, [
      `<span>${ago(m.created_at)}</span>`,
      ackBadge, redeliv,
    ]);
  }).join("");
}

function renderHeartbeat(hb) {
  const el = $("heartbeat");
  if (!hb || hb.last_seen === null) {
    el.className = "heartbeat none";
    el.textContent = "Coder not connected";
    return;
  }
  const sec = hb.freshness_seconds;
  el.textContent = `Coder seen ${sec}s ago`;
  if (sec < 30) el.className = "heartbeat fresh";
  else if (sec < 60) el.className = "heartbeat warm";
  else el.className = "heartbeat stale";
}

async function tick() {
  try {
    const r = await fetch("/api/dashboard/state");
    if (r.status === 401) { location.href = "/dashboard/login"; return; }
    if (!r.ok) throw new Error("status " + r.status);
    $("connlost").classList.remove("show");
    const s = await r.json();

    const m = s.active_mission;
    if (m) {
      $("mission-name").textContent = m.name;
      $("mission-status").textContent = m.status;
      $("mission-status").className = "badge " + m.status;
      $("mission-meta").textContent = `mission ${m.mission_id.slice(0,8)} · created ${ago(m.created_at)}`;
      fullSpec = m.spec || "";
      const preview = m.spec_preview || "";
      $("spec-preview").textContent = specExpanded ? fullSpec : preview;
      $("spec-toggle").style.display = fullSpec.length > preview.length ? "inline-block" : "none";
      $("spec-toggle").textContent = specExpanded ? "Collapse" : "Show full spec";
    } else {
      $("mission-name").textContent = "No active mission";
      $("mission-status").textContent = "none";
      $("mission-status").className = "badge none";
      $("mission-meta").textContent = "Planner has not created a mission yet.";
      $("spec-preview").textContent = "";
      $("spec-toggle").style.display = "none";
    }
    renderHeartbeat(s.coder_heartbeat);

    $("server-info").textContent =
      `server v${s.server_info.server_version} · catalog ${s.server_info.tools_catalog_hash} · up since ${ago(s.server_info.started_at)}`;

    $("questions").innerHTML = renderQuestions(s.pending_questions || []);
    $("summaries").innerHTML = renderSummaries(s.pending_summaries || []);
    $("msgs-c2p").innerHTML = renderMessages((s.messages && s.messages.coder_to_planner) || []);
    $("msgs-p2c").innerHTML = renderMessages((s.messages && s.messages.planner_to_coder) || []);
  } catch (e) {
    $("connlost").classList.add("show");
  }
}

$("spec-toggle").addEventListener("click", () => {
  specExpanded = !specExpanded;
  $("spec-preview").classList.toggle("expanded", specExpanded);
  $("spec-preview").textContent = specExpanded ? fullSpec : fullSpec.slice(0, 300);
  $("spec-toggle").textContent = specExpanded ? "Collapse" : "Show full spec";
});

tick();
setInterval(tick, POLL_MS);
</script>
</body>
</html>
"""


LOGIN_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>AgentsHive Dashboard — Login</title>
<style>
body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
       background: #0e0f12; color: #d6d9e0; margin: 0;
       display: flex; align-items: center; justify-content: center; min-height: 100vh; }
.card { background: #16181d; border: 1px solid #232732; border-radius: 8px;
        padding: 28px 32px; width: 360px; }
h1 { font-size: 18px; margin: 0 0 16px 0; font-weight: 600; }
label { display: block; font-size: 12px; color: #9aa0ab; margin-bottom: 6px; text-transform: uppercase; letter-spacing: 0.04em; }
input { width: 100%; box-sizing: border-box; padding: 10px 12px; font-size: 14px;
        background: #0e0f12; color: #d6d9e0; border: 1px solid #232732; border-radius: 6px;
        font-family: ui-monospace, SFMono-Regular, monospace; }
button { width: 100%; margin-top: 14px; padding: 10px 12px; background: #2a3a5a; color: #d6d9e0;
         border: 1px solid #3a4a6a; border-radius: 6px; font-size: 14px; cursor: pointer; }
button:hover { background: #3a4a6a; }
.error { color: #ff8a78; margin-top: 12px; font-size: 13px; }
.help { color: #6a6f7a; margin-top: 16px; font-size: 12px; line-height: 1.4; }
</style>
</head>
<body>
<form class="card" method="post" action="/dashboard/login">
  <h1>AgentsHive Dashboard</h1>
  <label for="api_key">API Key</label>
  <input id="api_key" type="password" name="api_key" autofocus autocomplete="current-password" required>
  <button type="submit">Sign in</button>
  {error_block}
  <div class="help">Paste the AGENTSHIVE_API_KEY env value. Session valid 12 hours.</div>
</form>
</body>
</html>
"""


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
