"""v2.x admin/superuser router — the ONE intentional cross-tenant surface.

Every endpoint calls is_admin() FIRST and 403s otherwise — UI hiding is not
security. is_admin() is verified-JWT based (app_metadata.role == "admin" OR
email == ADMIN_EMAIL). The service_role key is used SERVER-SIDE ONLY to reach
the Supabase Auth admin API; it is never exposed to the renderer.

Mounted at /admin/*; those paths are in BearerAuthMiddleware.PUBLIC_PATHS so the
legacy-shared-key-only outer middleware lets the admin's Supabase token through
to the per-handler is_admin() gate (same pattern as the dashboard routes).
"""

import os
import re

import httpx
from sqlmodel import Session, select
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from .config import Settings
from .db import (
    CoderHeartbeat,
    Message,
    Mission,
    OAuthAccessToken,
    OAuthAuthorizationCode,
    OAuthRefreshToken,
    Project,
    Question,
    Summary,
    Tenant,
    VALID_PLANS,
    get_engine,
    get_or_create_tenant,
)
from .tenant import LEGACY_TENANT, UNAUTHENTICATED_TENANT, current_identity, is_admin

# Supabase subs are UUIDs; guard destructive ops so a sentinel can never be a target.
_UUID_RE = re.compile(r"^[0-9a-fA-F-]{20,}$")

# Tables holding per-tenant data, deleted (scoped to one sub) on hard-remove.
_CASCADE_TABLES = [Project, Mission, Question, Summary, Message, CoderHeartbeat,
                   OAuthAccessToken, OAuthRefreshToken, OAuthAuthorizationCode]


def _supabase_base() -> str:
    return os.environ.get("SUPABASE_URL", "").rstrip("/")


def _supabase_admin_headers() -> dict:
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
    return {"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json"}


def _forbidden() -> Response:
    return JSONResponse({"error": "forbidden — admin only"}, status_code=403)


async def _read_json(request: Request) -> dict:
    try:
        return await request.json()
    except Exception:
        return {}


def _list_supabase_users() -> list[dict]:
    base = _supabase_base()
    if not base:
        return []
    headers = _supabase_admin_headers()
    out: list[dict] = []
    page = 1
    while True:
        r = httpx.get(f"{base}/auth/v1/admin/users", params={"page": page, "per_page": 200}, headers=headers, timeout=15)
        r.raise_for_status()
        body = r.json()
        users = body.get("users", body if isinstance(body, list) else [])
        out.extend(users)
        if not users or len(users) < 200:
            break
        page += 1
    return out


def _get_supabase_user(sub: str) -> dict | None:
    base = _supabase_base()
    if not base:
        return None
    try:
        r = httpx.get(f"{base}/auth/v1/admin/users/{sub}", headers=_supabase_admin_headers(), timeout=15)
        return r.json() if r.status_code == 200 else None
    except Exception:
        return None


def _self_or_admin_block(sub: str, target_user: dict | None) -> str | None:
    """Return a refusal reason if a destructive op (ban/remove) targets the
    calling admin's OWN account or ANY admin account, else None. Prevents
    self-lockout / self-deletion and admins nuking each other (esp. the operator).
    Pure (no I/O) so it's unit-testable; the caller fetches target_user first.
    """
    ident = current_identity() or {}
    if sub and sub == ident.get("sub"):
        return "refusing to ban/remove your own admin account"
    if target_user:
        meta = target_user.get("app_metadata") or {}
        admin_email = (os.environ.get("ADMIN_EMAIL") or "").strip().lower()
        if meta.get("role") == "admin" or (
            admin_email and (target_user.get("email") or "").strip().lower() == admin_email
        ):
            return "refusing to ban/remove an admin account"
    return None


def _counts(session: Session, sub: str) -> tuple[int, int]:
    pc = len(session.exec(select(Project.id).where(Project.tenant_id == sub)).all())
    mc = len(session.exec(select(Mission.id).where(Mission.tenant_id == sub)).all())
    return pc, mc


def _make_list_users(_settings: Settings):
    async def handler(request: Request) -> Response:
        if not is_admin():
            return _forbidden()
        try:
            su = _list_supabase_users()
        except Exception as e:
            return JSONResponse({"error": f"supabase admin api error: {e}"}, status_code=502)
        with Session(get_engine()) as session:
            trows = {t.tenant_id: t for t in session.exec(select(Tenant)).all()}
            result = []
            for u in su:
                sub = u.get("id")
                meta = u.get("app_metadata") or {}
                t = trows.get(sub)
                pc, mc = _counts(session, sub)
                result.append({
                    "sub": sub,
                    "email": u.get("email"),
                    "role": meta.get("role"),
                    "plan": (t.plan if t else (meta.get("plan") or "free")),
                    "subscription_status": (t.subscription_status if t else "none"),
                    "trial_reports_used": (t.trial_reports_used if t else 0),
                    "banned": (bool(t.banned) if t else bool(u.get("banned_until"))),
                    "created_at": u.get("created_at"),
                    "project_count": pc,
                    "mission_count": mc,
                })
        return JSONResponse({"users": result})
    return handler


def _set_supabase_ban(sub: str, banned: bool) -> None:
    base = _supabase_base()
    if not base:
        return
    # GoTrue admin: ban_duration "none" clears a ban; a long duration sets one.
    httpx.put(
        f"{base}/auth/v1/admin/users/{sub}",
        headers=_supabase_admin_headers(),
        json={"ban_duration": "876000h" if banned else "none"},
        timeout=15,
    )


def _make_set_banned(_settings: Settings, banned: bool):
    async def handler(request: Request) -> Response:
        if not is_admin():
            return _forbidden()
        sub = request.path_params.get("sub", "")
        if not _UUID_RE.match(sub):
            return JSONResponse({"error": "invalid user id"}, status_code=400)
        # Banning is destructive (immediate lockout) — protect self + other admins.
        # Unbanning is the recovery path, so it's always allowed.
        if banned:
            block = _self_or_admin_block(sub, _get_supabase_user(sub))
            if block:
                return JSONResponse({"error": block}, status_code=400)
        with Session(get_engine()) as session:
            t = get_or_create_tenant(session, sub)
            t.banned = banned
            session.add(t)
            session.commit()
        # Belt-and-suspenders: also set the Supabase ban so new logins are blocked
        # (our DB flag is what makes it immediate on existing tokens via load_access_token).
        try:
            _set_supabase_ban(sub, banned)
        except Exception:
            pass  # DB flag already enforces it; Supabase ban is supplementary
        return JSONResponse({"ok": True, "sub": sub, "banned": banned})
    return handler


def _make_set_plan(_settings: Settings):
    async def handler(request: Request) -> Response:
        if not is_admin():
            return _forbidden()
        sub = request.path_params.get("sub", "")
        if not _UUID_RE.match(sub):
            return JSONResponse({"error": "invalid user id"}, status_code=400)
        body = await _read_json(request)
        plan = (body.get("plan") or "").strip()
        if plan not in VALID_PLANS:
            return JSONResponse({"error": f"plan must be one of {sorted(VALID_PLANS)}"}, status_code=400)
        with Session(get_engine()) as session:
            t = get_or_create_tenant(session, sub)
            t.plan = plan
            session.add(t)
            session.commit()
        # DB Tenant.plan is the authoritative source the trial gate reads; we don't
        # mirror to Supabase app_metadata here to avoid clobbering other claims.
        return JSONResponse({"ok": True, "sub": sub, "plan": plan})
    return handler


def _make_remove_user(_settings: Settings):
    async def handler(request: Request) -> Response:
        if not is_admin():
            return _forbidden()
        sub = request.path_params.get("sub", "")
        # Refuse to ever target a reserved sentinel — only a real Supabase sub.
        if sub in (LEGACY_TENANT, UNAUTHENTICATED_TENANT) or not _UUID_RE.match(sub):
            return JSONResponse({"error": "refusing to remove a non-user/reserved tenant"}, status_code=400)
        # Refuse self-deletion and removing other admins (incl. the operator).
        block = _self_or_admin_block(sub, _get_supabase_user(sub))
        if block:
            return JSONResponse({"error": block}, status_code=400)
        # 1) delete the Supabase auth user (best-effort; report if it fails).
        supabase_err = None
        base = _supabase_base()
        if base:
            try:
                r = httpx.delete(f"{base}/auth/v1/admin/users/{sub}", headers=_supabase_admin_headers(), timeout=15)
                if r.status_code not in (200, 204):
                    supabase_err = f"HTTP {r.status_code}: {r.text[:160]}"
            except Exception as e:
                supabase_err = str(e)
        # 2) cascade-delete this sub's tenant data — STRICTLY this one tenant_id.
        deleted = {}
        with Session(get_engine()) as session:
            for model in _CASCADE_TABLES:
                rows = session.exec(select(model).where(model.tenant_id == sub)).all()
                for row in rows:
                    session.delete(row)
                deleted[model.__name__] = len(rows)
            trow = session.get(Tenant, sub)
            if trow is not None:
                session.delete(trow)
                deleted["Tenant"] = 1
            session.commit()
        return JSONResponse({"ok": True, "sub": sub, "deleted": deleted, "supabase_error": supabase_err})
    return handler


def _admin_guard(handler):
    """Structural is_admin() gate applied to EVERY /admin route at registration.

    Belt-and-suspenders over the per-handler checks: even if a future admin route
    forgets its own is_admin() call, this wrapper 403s non-admins first. /admin/*
    is in BearerAuthMiddleware.PUBLIC_PATHS, so this is the real authz boundary.
    """
    async def wrapped(request: Request) -> Response:
        if not is_admin():
            return _forbidden()
        return await handler(request)
    return wrapped


def register_admin_routes(app, settings: Settings) -> None:
    """Mount the /admin/* router. Every handler is wrapped in _admin_guard so the
    is_admin() gate is structural, not per-handler-remembered."""
    routes = [
        ("/admin/users", _make_list_users(settings), ["GET"]),
        ("/admin/users/{sub}/ban", _make_set_banned(settings, True), ["POST"]),
        ("/admin/users/{sub}/unban", _make_set_banned(settings, False), ["POST"]),
        ("/admin/users/{sub}/plan", _make_set_plan(settings), ["POST"]),
        ("/admin/users/{sub}/remove", _make_remove_user(settings), ["POST"]),
    ]
    for path, handler, methods in routes:
        app.router.routes.append(Route(path, _admin_guard(handler), methods=methods))
