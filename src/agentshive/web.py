"""v2.x companion webapp API (/web/*).

A thin, tenant-scoped HTTP+SSE surface so a browser (the operator's phone) can
chat with the agents running in THEIR desktop app — routed THROUGH the server
because the desktop isn't internet-reachable. The desktop is the relay: it polls
/web/inbound for web→agent messages addressed to its agents, injects each as a
local turn, and posts the response back via /web/relay; it publishes its agent
roster via /web/presence. The webapp reads /web/agents + /web/conversation and
streams new responses via /web/stream.

SECURITY — the #1 property: every endpoint is Supabase-JWT authed (tenant = the
token's `sub`) and scoped to that tenant. /web/* is in BearerAuthMiddleware's
PUBLIC_PATHS, so the per-request Supabase check enforced by _web_guard is the
SOLE gate — it wraps EVERY route (a future /web route can't ship unauthed). The
desktop authenticates as the operator's OWN Supabase tenant, and /web/inbound is
scoped to that tenant, so a web client can only ever reach its own agents — no
cross-tenant turn injection is possible.

Delivery: web_to_agent uses at-least-once semantics (delivered_at + redelivery_
count, mirroring the inbox) so the desktop never re-injects an acked message and
a crash-before-ack re-delivers.
"""

import asyncio
import base64
import hashlib
import json
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlmodel import Session, select
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from .config import Settings
from .db import (
    AgentToken,
    Message,
    Project,
    SyncedConversation,
    SyncedMessage,
    Tenant,  # noqa: F401  (imported for clarity; used via get_or_create_tenant)
    WebAgentPresence,
    cloud_sync_enabled,
    get_engine,
    get_or_create_tenant,
)
from .tenant import current_identity, current_tenant
from .tools import MAX_TEXT_LEN, _validate_text

WEB_TO_AGENT = "web_to_agent"
AGENT_TO_WEB = "agent_to_web"
PRESENCE_TTL_SECONDS = 30  # desktop online if it published within this window
# Per-message transcript text cap (defensive — a single synced message shouldn't
# blow up a row). Generous vs MAX_TEXT_LEN since transcripts hold full agent turns.
MAX_SYNC_TEXT_LEN = 500_000


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(d: datetime) -> datetime:
    return d if d.tzinfo is not None else d.replace(tzinfo=timezone.utc)


def _web_project_id(session: Session, slug: str) -> Optional[str]:
    """Resolve (current tenant, slug) → project id — the tenancy chokepoint for
    the web surface. Returns None if no such project for this tenant."""
    if not slug:
        return None
    row = session.exec(
        select(Project).where(Project.slug == slug, Project.tenant_id == current_tenant())
    ).first()
    return row.id if row is not None else None


def _web_msg_dict(m: Message) -> dict[str, Any]:
    return {
        "message_id": m.id,
        "direction": m.direction,
        "body": m.body,
        "agent_key": m.agent_key,
        "parent_id": m.parent_id,
        "created_at": m.created_at.isoformat(),
    }


# ---------- module-level helpers (testable; tenant-scoped via current_tenant) ----------

def do_web_send(slug: str, agent_key: Optional[str], body: str) -> dict[str, Any]:
    """Webapp → agent. Inserts a web_to_agent message addressed to agent_key in the
    tenant's project. agent_key=None means 'the project's Planner' (desktop resolves)."""
    err = _validate_text(body, "body", MAX_TEXT_LEN)
    if err:
        return err
    with Session(get_engine()) as session:
        pid = _web_project_id(session, slug)
        if pid is None:
            return {"error": f"project '{slug}' not found for this account"}
        m = Message(
            direction=WEB_TO_AGENT,
            tenant_id=current_tenant(),
            project_id=pid,
            body=body,
            agent_key=agent_key,
        )
        session.add(m)
        session.commit()
        session.refresh(m)
        return _web_msg_dict(m)


def fetch_web_inbound(limit: int = 20) -> list[dict[str, Any]]:
    """Desktop relay: oldest UNACKED web_to_agent messages for this tenant. Bumps
    redelivery_count (at-least-once); the desktop acks after injecting the turn."""
    out: list[dict[str, Any]] = []
    with Session(get_engine()) as session:
        rows = session.exec(
            select(Message)
            .where(
                Message.direction == WEB_TO_AGENT,
                Message.tenant_id == current_tenant(),
                Message.delivered_at.is_(None),
            )
            .order_by(Message.created_at)
            .limit(max(1, min(limit, 100)))
        ).all()
        slug_cache: dict[str, Optional[str]] = {}
        for m in rows:
            m.redelivery_count = (m.redelivery_count or 0) + 1
            session.add(m)
            if m.project_id not in slug_cache:
                proj = session.get(Project, m.project_id) if m.project_id else None
                slug_cache[m.project_id] = proj.slug if proj else None
            d = _web_msg_dict(m)
            d["project_slug"] = slug_cache.get(m.project_id)
            out.append(d)
        session.commit()
    return out


def do_web_ack(message_id: str) -> dict[str, Any]:
    """Desktop relay: acknowledge a web_to_agent message (tenant-scoped) after the
    desktop has injected the turn. EPHEMERAL: the row is DELETED on ack (not just
    stamped) to keep the cloud footprint minimal — there's a real ack here so the
    at-least-once guarantee holds (a crash before ack leaves the row for redelivery).
    Idempotent (a second ack just no-ops). NOTE: agent_to_web purge is DEFERRED to
    the webapp slice — its SSE consumer has no per-message ack yet, so purging it now
    risks dropping in-flight relay messages; design that purge with the consumer."""
    with Session(get_engine()) as session:
        m = session.get(Message, message_id)
        if m is None or m.tenant_id != current_tenant() or m.direction != WEB_TO_AGENT:
            return {"error": "no such message"}
        session.delete(m)
        session.commit()
        return {"ok": True, "message_id": message_id}


def do_web_relay(parent_id: Optional[str], slug: str, agent_key: Optional[str], body: str) -> dict[str, Any]:
    """Desktop relay: post an agent's response back to the webapp (agent_to_web),
    correlated to the originating web_to_agent via parent_id."""
    err = _validate_text(body, "body", MAX_TEXT_LEN)
    if err:
        return err
    with Session(get_engine()) as session:
        pid = _web_project_id(session, slug)
        # parent_id, if given, must belong to this tenant (defense-in-depth).
        if parent_id is not None:
            parent = session.get(Message, parent_id)
            if parent is not None and parent.tenant_id != current_tenant():
                return {"error": "invalid parent_id"}
        m = Message(
            direction=AGENT_TO_WEB,
            tenant_id=current_tenant(),
            project_id=pid,
            body=body,
            agent_key=agent_key,
            parent_id=parent_id,
        )
        session.add(m)
        session.commit()
        session.refresh(m)
        return _web_msg_dict(m)


def do_web_relay_ack(message_id: str) -> dict[str, Any]:
    """Webapp → server: acknowledge an agent_to_web message the web client has
    CONSUMED (rendered). EPHEMERAL: the row is DELETED on this confirmed-consume
    ack — not a blind TTL — so an in-flight relay message is never dropped before
    the client has it. Tenant-scoped + direction-checked. Idempotent. (Pairs with
    the web_to_agent purge in do_web_ack; together the relay channel is ephemeral
    while the durable history lives in the SyncedMessage transcript store.)"""
    with Session(get_engine()) as session:
        m = session.get(Message, message_id)
        if m is None or m.tenant_id != current_tenant() or m.direction != AGENT_TO_WEB:
            return {"error": "no such message"}
        session.delete(m)
        session.commit()
        return {"ok": True, "message_id": message_id}


def upsert_presence(slug: str, agents: list[dict[str, Any]]) -> dict[str, Any]:
    """Desktop relay: publish/refresh the tenant's agent roster + heartbeat."""
    now = _utcnow()
    tenant = current_tenant()
    with Session(get_engine()) as session:
        pid = _web_project_id(session, slug)
        n = 0
        for a in agents or []:
            key = a.get("agent_key")
            if not key:
                continue
            row = session.get(WebAgentPresence, (tenant, key))
            if row is None:
                row = WebAgentPresence(tenant_id=tenant, agent_key=key)
            row.project_id = pid
            row.project_slug = slug
            row.label = a.get("label")
            row.role = a.get("role")
            row.cli = a.get("cli")
            row.status = a.get("status")
            row.last_seen = now
            session.add(row)
            n += 1
        session.commit()
        return {"ok": True, "count": n}


def list_agents() -> list[dict[str, Any]]:
    """Webapp: the tenant's agent roster with online/offline (last_seen freshness)."""
    now = _utcnow()
    with Session(get_engine()) as session:
        rows = session.exec(
            select(WebAgentPresence).where(WebAgentPresence.tenant_id == current_tenant())
        ).all()
        out = []
        for r in rows:
            fresh = (now - _aware(r.last_seen)).total_seconds() <= PRESENCE_TTL_SECONDS
            out.append({
                "agent_key": r.agent_key,
                "project_slug": r.project_slug,
                "label": r.label,
                "role": r.role,
                "cli": r.cli,
                "status": r.status,
                "online": fresh,
                "last_seen": _aware(r.last_seen).isoformat(),
            })
        return out


def fetch_conversation(slug: str, agent_key: Optional[str], limit: int = 100) -> list[dict[str, Any]]:
    """Webapp: the conversation for one agent — the user's web_to_agent sends + the
    agent_to_web responses, oldest first, tenant+project scoped."""
    with Session(get_engine()) as session:
        pid = _web_project_id(session, slug)
        if pid is None:
            return []
        stmt = select(Message).where(
            Message.tenant_id == current_tenant(),
            Message.project_id == pid,
            Message.direction.in_((WEB_TO_AGENT, AGENT_TO_WEB)),
        )
        if agent_key:
            stmt = stmt.where(Message.agent_key == agent_key)
        rows = session.exec(stmt.order_by(Message.created_at.desc()).limit(max(1, min(limit, 500)))).all()
        return [_web_msg_dict(m) for m in reversed(rows)]


def fetch_agent_to_web_since(tenant_id: str, after: Optional[datetime]) -> list[Message]:
    """SSE helper: agent_to_web rows for an EXPLICIT tenant created after `after`.
    Takes tenant explicitly (the SSE loop captures it at connect — it must not rely
    on the request contextvar, which resets once the streaming response begins)."""
    with Session(get_engine()) as session:
        stmt = select(Message).where(
            Message.tenant_id == tenant_id,
            Message.direction == AGENT_TO_WEB,
        )
        if after is not None:
            stmt = stmt.where(Message.created_at > after)
        return list(session.exec(stmt.order_by(Message.created_at)).all())


# ---------- v2.x Cloud Sync (opt-in): entitlements + transcript push/pull --------

def tenant_cloud_sync_enabled() -> bool:
    """Resolve the CURRENT tenant's Cloud Sync entitlement (row-based)."""
    with Session(get_engine()) as session:
        row = get_or_create_tenant(session, current_tenant())
        return cloud_sync_enabled(row)


def do_web_me() -> dict[str, Any]:
    """The current tenant's identity + entitlements. The desktop reads this to gate
    the Cloud Sync toggle (enabled only when cloud_sync resolves True)."""
    ident = current_identity() or {}
    tenant = current_tenant()
    with Session(get_engine()) as session:
        row = get_or_create_tenant(session, tenant, email=ident.get("email"))
        return {
            "sub": tenant,
            "email": row.email or ident.get("email"),
            "plan": row.plan,
            "cloud_sync": cloud_sync_enabled(row),
        }


# ---------- v2.x long-lived agent tokens (`ahat_...`) ----------
# Operator-minted, tenant-bound, never expires. Replaces the 1h Supabase JWT path
# on Coder MCP subprocesses (the source of the recurring "Auth required" break).
# See db.AgentToken for the model + format.

# Defensive cap on operator-supplied label. /^[\w \-.@]+$/ explicitly excludes
# shell metas + most punctuation so a label can never feed into anything dangerous.
_AGENT_TOKEN_LABEL_MAX = 80
_AGENT_TOKEN_LABEL_RE = re.compile(r"^[\w \-.@]+$")
# Defensive rate-limit: legitimate use is one-per-machine. A tenant minting 10 in
# an hour is either testing or compromised — either way, hard-stop.
_AGENT_TOKEN_MAX_PER_HOUR = 10


def _mint_agent_token_secret() -> tuple[str, str, str]:
    """Generate a fresh (full_token, prefix, secret_hash) tuple.

    Format: `ahat_` + 40 chars url-safe base64 (no padding) of secrets.token_bytes(30).
    30 bytes ⇒ 240 bits of entropy — well above any brute-force concern; the sha256
    storage means a DB leak surrenders the hash, not the live bearer. prefix = first
    8 chars of the base64 body so the UI can render "ahat_abc12345…" for revoke UX
    without ever revealing the secret half.
    """
    raw = secrets.token_bytes(30)
    body = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")  # 40 chars
    full = "ahat_" + body
    prefix = body[:8]
    secret_hash = hashlib.sha256(full.encode("utf-8")).hexdigest()
    return full, prefix, secret_hash


def do_create_agent_token(label: str) -> tuple[dict[str, Any], int]:
    """POST /web/agent-tokens. Validates label, rate-limits, mints + stores hash,
    returns the FULL plaintext exactly ONCE. Returns (body, status_code)."""
    label = (label or "").strip()
    if not label:
        return {"error": "label is required"}, 400
    if len(label) > _AGENT_TOKEN_LABEL_MAX:
        return {"error": f"label exceeds {_AGENT_TOKEN_LABEL_MAX} chars"}, 400
    if not _AGENT_TOKEN_LABEL_RE.match(label):
        return {"error": "label contains forbidden characters"}, 400
    tenant = current_tenant()
    with Session(get_engine()) as session:
        # Ensure the Tenant row exists (banned check + admin UX cache key).
        get_or_create_tenant(session, tenant)
        recent_cutoff = _utcnow() - timedelta(hours=1)
        recent = session.exec(
            select(AgentToken).where(
                AgentToken.tenant_id == tenant,
                AgentToken.created_at > recent_cutoff,
            )
        ).all()
        if len(recent) >= _AGENT_TOKEN_MAX_PER_HOUR:
            return {"error": "rate limit: 10 agent tokens per hour per tenant"}, 429
        full, prefix, secret_hash = _mint_agent_token_secret()
        row = AgentToken(
            tenant_id=tenant, label=label, secret_hash=secret_hash, prefix=prefix,
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        return ({
            "id": row.id,
            "label": row.label,
            "prefix": row.prefix,
            "token": full,  # ONLY time the plaintext is ever returned
            "created_at": row.created_at.isoformat(),
        }, 200)


def do_list_agent_tokens() -> dict[str, Any]:
    """GET /web/agent-tokens. Returns the tenant's tokens; NEVER returns the secret."""
    tenant = current_tenant()
    with Session(get_engine()) as session:
        rows = session.exec(
            select(AgentToken)
            .where(AgentToken.tenant_id == tenant)
            .order_by(AgentToken.created_at)
        ).all()
        return {
            "tokens": [
                {
                    "id": r.id,
                    "label": r.label,
                    "prefix": r.prefix,
                    "created_at": r.created_at.isoformat(),
                    "last_used_at": r.last_used_at.isoformat() if r.last_used_at else None,
                    "revoked_at": r.revoked_at.isoformat() if r.revoked_at else None,
                    "revoked": r.revoked_at is not None,
                }
                for r in rows
            ]
        }


def do_revoke_agent_token(token_id: str) -> tuple[dict[str, Any], int]:
    """DELETE /web/agent-tokens/{id}. 404 cross-tenant (don't leak existence).
    Soft-delete: sets revoked_at. Idempotent — revoking an already-revoked row is OK."""
    if not token_id:
        return {"error": "not found"}, 404
    tenant = current_tenant()
    with Session(get_engine()) as session:
        row = session.exec(
            select(AgentToken).where(
                AgentToken.id == token_id,
                AgentToken.tenant_id == tenant,
            )
        ).first()
        if row is None:
            return {"error": "not found"}, 404
        if row.revoked_at is None:
            row.revoked_at = _utcnow()
            session.add(row)
            session.commit()
        return ({"ok": True, "id": row.id, "revoked_at": row.revoked_at.isoformat()}, 200)


def do_sync_push(slug: str, agent_id: str, label: Optional[str], role: Optional[str],
                 cli: Optional[str], messages: list[dict[str, Any]]) -> dict[str, Any]:
    """Desktop → cloud: idempotent upsert of one agent's transcript. ENTITLEMENT-
    GATED + PRIVACY-FIRST: writes NOTHING when Cloud Sync is off (defense in depth —
    even a direct call can't leak transcripts). `messages` = [{uuid, idx, role, text,
    tool_calls, tokens, created_at}]. Upsert identity is (tenant_id, uuid); render
    order is `idx`. Last push wins (LWW via updated_at = now)."""
    if not tenant_cloud_sync_enabled():
        return {"error": "Cloud Sync is not enabled for this account.", "gated": True}
    if not slug or not agent_id:
        return {"error": "project and agent_id are required"}
    tenant = current_tenant()
    now = _utcnow()
    with Session(get_engine()) as session:
        convo = session.exec(
            select(SyncedConversation).where(
                SyncedConversation.tenant_id == tenant,
                SyncedConversation.project_slug == slug,
                SyncedConversation.agent_id == agent_id,
            )
        ).first()
        if convo is None:
            convo = SyncedConversation(tenant_id=tenant, project_slug=slug, agent_id=agent_id, created_at=now)
        if label is not None:
            convo.label = label
        if role is not None:
            convo.role = role
        if cli is not None:
            convo.cli = cli
        convo.updated_at = now
        session.add(convo)
        synced = 0
        for m in (messages or []):
            muuid = str(m.get("uuid") or "").strip()
            if not muuid:
                continue  # uuid is the upsert identity — skip anything without one
            existing = session.exec(
                select(SyncedMessage).where(
                    SyncedMessage.tenant_id == tenant,
                    SyncedMessage.msg_uuid == muuid,
                )
            ).first()
            row = existing or SyncedMessage(tenant_id=tenant, msg_uuid=muuid, created_at=now)
            row.project_slug = slug
            row.agent_id = agent_id
            row.idx = int(m.get("idx") or 0)
            row.role = str(m.get("role") or "assistant")
            row.text = str(m.get("text") or "")[:MAX_SYNC_TEXT_LEN]
            tc = m.get("tool_calls")
            row.tool_calls = tc if isinstance(tc, list) else None
            tk = m.get("tokens")
            row.tokens = tk if isinstance(tk, dict) else None
            row.updated_at = now
            session.add(row)
            synced += 1
        session.commit()
        return {"ok": True, "agent_id": agent_id, "synced": synced}


def do_sync_pull(slug: str, since_iso: Optional[str] = None) -> dict[str, Any]:
    """Cloud → desktop: the tenant's transcripts for a project, optionally only those
    updated after `since` (ISO8601 cursor). ENTITLEMENT-GATED. Returns conversations
    (with messages ordered by idx) + a `cursor` (max updated_at seen) for the next
    incremental pull. Strictly tenant-scoped."""
    if not tenant_cloud_sync_enabled():
        return {"error": "Cloud Sync is not enabled for this account.", "gated": True}
    tenant = current_tenant()
    after: Optional[datetime] = None
    if since_iso:
        try:
            after = _aware(datetime.fromisoformat(since_iso))
        except Exception:
            after = None
    with Session(get_engine()) as session:
        convo_stmt = select(SyncedConversation).where(SyncedConversation.tenant_id == tenant)
        if slug:
            convo_stmt = convo_stmt.where(SyncedConversation.project_slug == slug)
        convos = session.exec(convo_stmt).all()

        msg_stmt = select(SyncedMessage).where(SyncedMessage.tenant_id == tenant)
        if slug:
            msg_stmt = msg_stmt.where(SyncedMessage.project_slug == slug)
        if after is not None:
            msg_stmt = msg_stmt.where(SyncedMessage.updated_at > after)
        msgs = session.exec(msg_stmt.order_by(SyncedMessage.agent_id, SyncedMessage.idx)).all()

        by_agent: dict[str, list[dict[str, Any]]] = {}
        max_updated = after
        for m in msgs:
            by_agent.setdefault(m.agent_id, []).append({
                "uuid": m.msg_uuid,
                "idx": m.idx,
                "role": m.role,
                "text": m.text,
                "tool_calls": m.tool_calls,
                "tokens": m.tokens,
                "created_at": _aware(m.created_at).isoformat(),
                "updated_at": _aware(m.updated_at).isoformat(),
            })
            mu = _aware(m.updated_at)
            if max_updated is None or mu > max_updated:
                max_updated = mu

        out_convos = []
        for c in convos:
            ms = by_agent.get(c.agent_id, [])
            if after is not None and not ms:
                continue  # incremental pull: skip conversations with nothing new
            out_convos.append({
                "agent_id": c.agent_id,
                "project_slug": c.project_slug,
                "label": c.label,
                "role": c.role,
                "cli": c.cli,
                "updated_at": _aware(c.updated_at).isoformat(),
                "messages": ms,
            })
        return {
            "conversations": out_convos,
            "cursor": max_updated.isoformat() if max_updated else None,
        }


# ---------- HTTP handlers (each wrapped in _web_guard at registration) ----------

async def _read_json(request: Request) -> dict:
    try:
        return await request.json()
    except Exception:
        return {}


def _forbidden() -> Response:
    return JSONResponse({"error": "forbidden — sign in"}, status_code=403)


def _web_guard(handler):
    """Structural gate on EVERY /web route: require a verified Supabase identity
    (tenant = sub). The legacy shared key / cookie are NOT accepted here — the web
    surface is Supabase-only. Since /web/* is in PUBLIC_PATHS, this is the sole
    auth boundary, applied once at registration so no route can forget it."""
    async def wrapped(request: Request) -> Response:
        if current_identity() is None:
            return _forbidden()
        return await handler(request)
    return wrapped


def _make_send(_s: Settings):
    async def h(request: Request) -> Response:
        body = await _read_json(request)
        slug = (body.get("project") or "").strip().lower()
        return JSONResponse(do_web_send(slug, body.get("agent_key"), body.get("body") or ""))
    return h


def _make_agents(_s: Settings):
    async def h(_request: Request) -> Response:
        return JSONResponse({"agents": list_agents()})
    return h


def _make_conversation(_s: Settings):
    async def h(request: Request) -> Response:
        slug = (request.query_params.get("project") or "").strip().lower()
        agent_key = request.query_params.get("agent_key") or None
        return JSONResponse({"messages": fetch_conversation(slug, agent_key)})
    return h


def _make_inbound(_s: Settings):
    async def h(_request: Request) -> Response:
        return JSONResponse({"messages": fetch_web_inbound()})
    return h


def _make_ack(_s: Settings):
    async def h(request: Request) -> Response:
        body = await _read_json(request)
        return JSONResponse(do_web_ack((body.get("message_id") or "").strip()))
    return h


def _make_relay_ack(_s: Settings):
    async def h(request: Request) -> Response:
        body = await _read_json(request)
        return JSONResponse(do_web_relay_ack((body.get("message_id") or "").strip()))
    return h


def _make_relay(_s: Settings):
    async def h(request: Request) -> Response:
        body = await _read_json(request)
        slug = (body.get("project") or "").strip().lower()
        return JSONResponse(do_web_relay(body.get("parent_id"), slug, body.get("agent_key"), body.get("body") or ""))
    return h


def _make_presence(_s: Settings):
    async def h(request: Request) -> Response:
        body = await _read_json(request)
        slug = (body.get("project") or "").strip().lower()
        agents = body.get("agents") if isinstance(body.get("agents"), list) else []
        return JSONResponse(upsert_presence(slug, agents))
    return h


def _make_me(_s: Settings):
    async def h(_request: Request) -> Response:
        return JSONResponse(do_web_me())
    return h


def _make_create_agent_token(_s: Settings):
    async def h(request: Request) -> Response:
        body = await _read_json(request)
        payload, status = do_create_agent_token((body.get("label") or "").strip() if isinstance(body, dict) else "")
        return JSONResponse(payload, status_code=status)
    return h


def _make_list_agent_tokens(_s: Settings):
    async def h(_request: Request) -> Response:
        return JSONResponse(do_list_agent_tokens())
    return h


def _make_revoke_agent_token(_s: Settings):
    async def h(request: Request) -> Response:
        token_id = (request.path_params.get("id") or "").strip()
        payload, status = do_revoke_agent_token(token_id)
        return JSONResponse(payload, status_code=status)
    return h


def _make_sync_push(_s: Settings):
    async def h(request: Request) -> Response:
        body = await _read_json(request)
        res = do_sync_push(
            (body.get("project") or "").strip().lower(),
            (body.get("agent_id") or "").strip(),
            body.get("label"),
            body.get("role"),
            body.get("cli"),
            body.get("messages") if isinstance(body.get("messages"), list) else [],
        )
        return JSONResponse(res, status_code=402 if res.get("gated") else 200)
    return h


def _make_sync_pull(_s: Settings):
    async def h(request: Request) -> Response:
        slug = (request.query_params.get("project") or "").strip().lower()
        since = request.query_params.get("since") or None
        res = do_sync_pull(slug, since)
        return JSONResponse(res, status_code=402 if res.get("gated") else 200)
    return h


def _make_stream(_s: Settings):
    KEEPALIVE = 20

    async def h(request: Request) -> Response:
        # Capture the tenant at connect — the streaming body runs after dispatch
        # returns, when the request contextvar may be reset, so we pass it
        # explicitly into the query helper.
        tenant = current_tenant()

        async def gen():
            cursor: Optional[datetime] = _utcnow()
            idle = 0
            try:
                yield b": connected\n\n"
                while True:
                    rows = fetch_agent_to_web_since(tenant, cursor)
                    if rows:
                        for m in rows:
                            cursor = _aware(m.created_at)
                            payload = json.dumps(_web_msg_dict(m))
                            yield f"event: message\ndata: {payload}\n\n".encode("utf-8")
                        idle = 0
                    else:
                        idle += 1
                    # keepalive every ~KEEPALIVE seconds of idle polling
                    if idle and idle % KEEPALIVE == 0:
                        yield b": keepalive\n\n"
                    await asyncio.sleep(1.0)
            except (asyncio.CancelledError, GeneratorExit):
                return

        return StreamingResponse(
            gen(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
        )
    return h


def register_web_routes(app, settings: Settings) -> None:
    """Mount /web/*; every handler is wrapped in _web_guard (Supabase-only)."""
    routes = [
        ("/web/agents", _make_agents(settings), ["GET"]),
        ("/web/conversation", _make_conversation(settings), ["GET"]),
        ("/web/message", _make_send(settings), ["POST"]),
        ("/web/stream", _make_stream(settings), ["GET"]),
        ("/web/inbound", _make_inbound(settings), ["GET"]),
        ("/web/ack", _make_ack(settings), ["POST"]),
        ("/web/relay", _make_relay(settings), ["POST"]),
        ("/web/relay-ack", _make_relay_ack(settings), ["POST"]),
        ("/web/presence", _make_presence(settings), ["POST"]),
        # v2.x Cloud Sync (opt-in): entitlements + transcript push/pull.
        ("/web/me", _make_me(settings), ["GET"]),
        ("/web/sync/push", _make_sync_push(settings), ["POST"]),
        ("/web/sync/pull", _make_sync_pull(settings), ["GET"]),
        # v2.x long-lived agent tokens — Supabase-JWT-guarded by _web_guard so the
        # tenant is always the operator's sub. POST mints + returns plaintext once;
        # GET lists (no secret); DELETE soft-revokes (404 cross-tenant — never leak).
        ("/web/agent-tokens", _make_create_agent_token(settings), ["POST"]),
        ("/web/agent-tokens", _make_list_agent_tokens(settings), ["GET"]),
        ("/web/agent-tokens/{id}", _make_revoke_agent_token(settings), ["DELETE"]),
    ]
    for path, handler, methods in routes:
        app.router.routes.append(Route(path, _web_guard(handler), methods=methods))
