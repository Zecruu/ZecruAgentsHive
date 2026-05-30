import hashlib
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from fastmcp import Context
from mcp.types import ToolListChangedNotification
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from . import __version__ as AGENTSHIVE_VERSION
from . import dashboard_events
from .config import Settings
from .db import (
    AgentPresence,
    CoderHeartbeat,
    Message,
    Mission,
    PLAN_PRO,
    PLAN_PRO_UNLIMITED,
    Project,
    Question,
    Summary,
    get_engine,
    get_or_create_tenant,
)
from .project import current_project, validate_coder_id, SLUG_PATTERN
from .tenant import (
    LEGACY_TENANT,
    UNAUTHENTICATED_TENANT,
    current_tenant,
    is_admin,
)


# v1.13: heartbeat throttle. _touch_coder writes a CoderHeartbeat row at most
# once per HEARTBEAT_MIN_INTERVAL_SECONDS per (project_id, coder_id) pair so a
# chatty Coder doesn't generate one write per tool call. Module-level constant
# so tests can monkeypatch it to 0 for "heartbeat updates on every call" cases.
HEARTBEAT_MIN_INTERVAL_SECONDS = 10

# v1.13: message_id form vs ISO timestamp form for the wait_for_planner_message
# `since` parameter. uuid4().hex is always 32 lowercase hex; anything else gets
# parsed as ISO 8601.
_MESSAGE_ID_REGEX = re.compile(r"^[a-f0-9]{32}$")


# Captured at module load. See get_server_info — surfaced as `started_at` so a client
# can detect "I am talking to a server that restarted since my last call." Combined
# with tools_catalog_hash drift, this gives cooperating clients a path to notice
# their cached tool list is stale without having to compare every tool name.
SERVER_STARTED_AT = datetime.now(timezone.utc)


def _compute_tools_catalog_hash(tool_names: list[str]) -> str:
    """Deterministic short fingerprint of the registered tool surface.

    sha256 over sorted-and-newline-joined tool names, truncated to 16 hex chars.
    16 hex chars = 64 bits, more than enough collision space to detect drift while
    staying short enough to display in a status line. Stable across calls within
    a deploy; changes only when the tool surface itself changes.
    """
    payload = "\n".join(sorted(tool_names)).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:16]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _resolve_since(since: Optional[str]) -> Optional[datetime]:
    """v1.13: parse the wait_for_planner_message `since` parameter.

    Accepts either an ISO 8601 timestamp string OR a message_id (32-char lowercase
    hex, matching uuid4().hex). Returns the corresponding `created_at` boundary as
    an aware UTC datetime, or None if the input should be ignored.

    Silent passthrough (return None) cases per the brief:
      - since is None or empty
      - since is malformed (neither parseable as ISO 8601 nor as message_id)
      - since is a message_id that doesn't exist in the DB
      - since is in the future
      - (since older than the earliest row is acceptable — the filter still
        applies; it just doesn't exclude anything, which is the same outcome
        as not passing since at all)

    Naive datetimes are coerced to UTC. The returned value is always aware.
    """
    if not since:
        return None
    candidate: Optional[datetime] = None
    if _MESSAGE_ID_REGEX.match(since):
        # message_id form — look up the row to get its created_at. Tenant-scoped:
        # another tenant's message id is treated as unknown (silent passthrough).
        with Session(get_engine()) as session:
            row = _tenant_get(session, Message, since)
            if row is None:
                return None  # unknown id — silent passthrough
            candidate = row.created_at
    else:
        try:
            candidate = datetime.fromisoformat(since)
        except (ValueError, TypeError):
            return None
    if candidate is None:
        return None
    if candidate.tzinfo is None:
        candidate = candidate.replace(tzinfo=timezone.utc)
    if candidate > _utcnow():
        return None  # future timestamp — silent passthrough
    return candidate


def _mission_dict(m: Mission) -> dict[str, Any]:
    return {
        "mission_id": m.id,
        "name": m.name,
        "spec": m.spec,
        "status": m.status,
        "created_at": m.created_at.isoformat(),
        "done_at": m.done_at.isoformat() if m.done_at else None,
        "coder_last_seen": m.coder_last_seen.isoformat() if m.coder_last_seen else None,
    }


def _foundation_dict(proj: Optional[Project]) -> Optional[dict[str, Any]]:
    """v2.x: the project's durable foundation mission (north-star goal), or None."""
    if proj is None or not proj.foundation_name:
        return None
    return {
        "name": proj.foundation_name,
        "spec": proj.foundation_spec,
        "set_at": proj.foundation_set_at.isoformat() if proj.foundation_set_at else None,
    }


def _question_dict(q: Question) -> dict[str, Any]:
    return {
        "question_id": q.id,
        "mission_id": q.mission_id,
        "body": q.body,
        "answer": q.answer,
        "created_at": q.created_at.isoformat(),
        "answered_at": q.answered_at.isoformat() if q.answered_at else None,
        # v1.11: who asked. None = legacy single-Coder mode.
        "coder_id": q.coder_id,
    }


def _summary_dict(s: Summary) -> dict[str, Any]:
    return {
        "summary_id": s.id,
        "mission_id": s.mission_id,
        "body": s.body,
        "response": s.response,
        "created_at": s.created_at.isoformat(),
        "responded_at": s.responded_at.isoformat() if s.responded_at else None,
        # v1.11: who submitted. None = legacy single-Coder mode.
        "coder_id": s.coder_id,
    }


def _message_dict(m: Message) -> dict[str, Any]:
    # redelivery_count surface is 0-indexed (matches docstring: 0 = first delivery,
    # positive = N readers saw this before you without acking). DB stays 1-indexed
    # internally because writes are simpler that way; we subtract 1 here so callers
    # see the semantic value. max(0, ...) handles the never-returned case where the
    # DB column is still 0 from the default.
    db_count = m.redelivery_count or 0
    return {
        "message_id": m.id,
        "mission_id": m.mission_id,
        "direction": m.direction,
        "body": m.body,
        "created_at": m.created_at.isoformat(),
        # delivered_at semantically means "acked_at" since v1.2 — see Message model docstring
        "delivered_at": m.delivered_at.isoformat() if m.delivered_at else None,
        "redelivery_count": max(0, db_count - 1),
        # v1.11: dual identity. coder_id = sender (set on coder_to_planner rows when
        # the Coder declared an id). target_coder_id = recipient filter on
        # planner_to_coder rows (None = broadcast, "A" = only Coder A consumes it).
        "coder_id": m.coder_id,
        "target_coder_id": m.target_coder_id,
    }


def _project_id(session: Session, slug: Optional[str] = None) -> Optional[str]:
    """v1.9: resolve the current request's project slug to its row id.
    v2.x: resolution is the tenancy CHOKEPOINT — it filters on (tenant_id, slug),
    so a slug under tenant A resolves to a different project_id than the same slug
    under tenant B, and every child query rooted here is transitively isolated.

    Defaults to the slug from the request-time ContextVar. Returns None if no
    project matches (tenant + slug), which callers treat as "no active mission".
    """
    s = slug if slug is not None else current_project()
    row = session.exec(
        select(Project).where(Project.slug == s, Project.tenant_id == current_tenant())
    ).first()
    return row.id if row is not None else None


def _tenant_get(session: Session, model_cls, row_id):
    """v2.x: get a row by its OWN id, but ONLY if it belongs to the current tenant.

    Closes the by-id IDOR gap the resolution chokepoint can't cover: tools that
    load a child row directly by a global id (answer_question, respond_to_summary,
    ack_message, is_mission_done, the wait-on-specific-row loops). Returns None if
    the row is missing OR owned by another tenant — callers already handle None as
    "no such row", so cross-tenant access is indistinguishable from not-found.
    """
    row = session.get(model_cls, row_id)
    if row is None:
        return None
    if getattr(row, "tenant_id", None) != current_tenant():
        return None
    return row


# v2.x trial/plan gate. The operator's "pro_unlimited" plan (and any future
# active Stripe subscription) → unlimited. A free tenant gets TRIAL_REPORT_LIMIT
# progress reports, then mission mutators are blocked with a structured
# trial_ended error the desktop can detect. The LEGACY tenant is ALWAYS exempt —
# it's the transitional shared-key path AND our own dogfood coordination channel,
# which must never be trial-gated. Stripe stays deferred; plans are admin-set.
TRIAL_REPORT_LIMIT = 2


def _check_plan_gate(count_report: bool = False) -> Optional[dict[str, Any]]:
    """Return a structured trial_ended error dict if the current tenant is blocked,
    else None. When count_report is True (submit_progress), increments the tenant's
    trial_reports_used on the allowed path."""
    tenant = current_tenant()
    if tenant in (LEGACY_TENANT, UNAUTHENTICATED_TENANT):
        # legacy = exempt (dogfood/transitional); unauthenticated resolves to no
        # project anyway and is gated upstream — not this gate's concern.
        return None
    # Admins are never trial-gated (tamper-proof: role rides in the verified JWT).
    # DB Tenant.plan is the single authoritative plan source for everyone else —
    # we deliberately do NOT honor an app_metadata.plan claim here, so a downgrade
    # can't lag a stale token.
    if is_admin():
        return None
    with Session(get_engine()) as session:
        row = get_or_create_tenant(session, tenant)
        if row.plan in (PLAN_PRO, PLAN_PRO_UNLIMITED) or row.subscription_status == "active":
            return None
        if row.trial_reports_used >= TRIAL_REPORT_LIMIT:
            return {
                "error": (
                    f"Free trial ended — you've used your {TRIAL_REPORT_LIMIT} trial "
                    "progress reports. Upgrade to keep going."
                ),
                "trial_ended": True,
                "plan": row.plan,
                "trial_reports_used": row.trial_reports_used,
            }
        if count_report:
            row.trial_reports_used += 1
            session.add(row)
            session.commit()
    return None


def _active_mission(session: Session, project_slug: Optional[str] = None) -> Optional[Mission]:
    """v1.9: scoped to the current request's project (or an explicit slug).

    Each project has at most one active mission (enforced by the
    one_active_mission_per_project partial unique index).
    """
    pid = _project_id(session, project_slug)
    if pid is None:
        return None
    return session.exec(
        select(Mission)
        .where(Mission.status == "active", Mission.project_id == pid)
        .order_by(Mission.created_at.desc())
    ).first()


# ---------- Input validation (v1.2 Feature 3) ----------
# Length caps live here, not in config, because they're protocol guarantees rather
# than per-deployment tunables. If anyone hits a wall against these, we'll move
# them to env vars then — until then a single source of truth keeps the surface flat.

MAX_NAME_LEN = 200
MAX_SPEC_LEN = 64 * 1024       # 64 KB
MAX_TEXT_LEN = 16 * 1024       # 16 KB — applies to question, summary, message body,
                               # answer, response. One cap simpler than seven nearly-equal ones.


def _validate_text(value: str, field_name: str, max_len: int) -> Optional[dict]:
    """Return an error dict if value is empty/whitespace or exceeds max_len; None if OK.

    Tool entry points call this and short-circuit on a non-None return so callers
    get a clean error before any DB write happens.
    """
    if not isinstance(value, str) or not value.strip():
        return {"error": f"{field_name} must be a non-empty string"}
    if len(value) > max_len:
        return {
            "error": (
                f"{field_name} exceeds maximum length of {max_len} characters "
                f"(got {len(value)})"
            )
        }
    return None


# v1.15: device hint allow-list. Used by validate_os_hint and surfaced via
# the Connected Coders dashboard panel as an OS icon.
ALLOWED_OS_HINTS = frozenset({"windows", "macos", "linux"})


def validate_project_scope(claimed_slug: Optional[str]) -> Optional[dict[str, Any]]:
    """v1.16: enforce that the caller's claimed project_slug matches the actual
    project this MCP request is routed to.

    The bug this prevents:
    - An agent has multiple AgentsHive MCP connectors loaded (e.g., a workspace-
      local one for project A AND a claude.ai cloud connector for project B).
    - The agent reads project A's mission spec, then calls submit_progress via
      the cloud connector — which routes to project B and silently corrupts
      B's mission state. v1.12's Step 0 (call get_project_info first) is
      advisory; this guard is enforced.

    Usage from a tool wrapper:

        err = validate_project_scope(project_slug)
        if err:
            return err   # caller propagates the error dict to the agent
        ... mutate ...

    Returns None when:
    - claimed_slug is None (caller didn't opt in; legacy behavior preserved)
    - claimed_slug equals the request's actual project (the happy path)

    Returns an error dict otherwise. The error message names BOTH the
    claimed slug AND the actual one so the agent can self-diagnose. Caller
    is expected to surface this to the requesting agent (do not raise — that
    bypasses the FastMCP error envelope on some clients).
    """
    if claimed_slug is None:
        return None
    actual = current_project()
    if claimed_slug == actual:
        return None
    return {
        "error": (
            f"project scope mismatch: caller claimed project_slug={claimed_slug!r} "
            f"but this MCP request is routed to project_slug={actual!r}. "
            "Refusing to mutate. This usually means the caller has multiple "
            "AgentsHive MCP connectors loaded and picked the wrong one for the "
            "spec they're executing. Call get_project_info() to verify which "
            "project the current connection is on, then either: (a) switch to "
            "the correct connector, or (b) update your project_slug arg to "
            "match the actual route."
        ),
        "claimed_project_slug": claimed_slug,
        "actual_project_slug": actual,
    }


def validate_os_hint(value: Optional[str]) -> None:
    """Validate an optional os_hint. None is legal (legacy / cloud Coder).

    Raises ValueError on a non-None value outside ALLOWED_OS_HINTS so a typo
    ("Windows", "darwin", "osx") fails loudly at the API boundary instead of
    silently rendering as an unknown OS in the dashboard.
    """
    if value is None:
        return
    if value not in ALLOWED_OS_HINTS:
        raise ValueError(
            f"os_hint must be one of {sorted(ALLOWED_OS_HINTS)} or None "
            f"(got {value!r})"
        )


def _touch_coder(
    session: Session,
    coder_id: Optional[str] = None,
    os_hint: Optional[str] = None,
) -> None:
    """Update the heartbeat trail for the Coder making this tool call.

    Called once per Coder-side tool invocation so the Planner can see whether the
    Coder process is alive without an explicit ping protocol. Called at the START
    of a tool, not inside per-iteration polling loops — one touch per call is the
    intended granularity ("the Coder placed this tool call N seconds ago").

    Two heartbeat trails since v1.13:
    - Legacy: Mission.coder_last_seen is always bumped on every Coder call (no
      coder_id needed). This is what the existing dashboard `coder_heartbeat`
      surface reads. Backwards compatible with pre-v1.13 single-Coder setups.
    - Per-Coder: if coder_id is supplied, upsert CoderHeartbeat(project_id,
      coder_id) → last_seen=now. Throttled — skip the write if the row was
      bumped within HEARTBEAT_MIN_INTERVAL_SECONDS so a chatty Coder doesn't
      generate one write per tool call. Surfaces to the Connected Coders
      dashboard panel even when the Coder has no Q/S/M yet.

    v1.15: os_hint, if supplied, is validated + persisted on the heartbeat
    row alongside coder_id. Only meaningful when coder_id is also passed (a
    bare os_hint with no coder_id has nothing to attach to). Updates the
    stored os_hint on every non-throttled write so a Coder moving between
    devices (rare) eventually reflects in the panel.

    No-op for the legacy trail if no mission is active. The per-Coder trail
    runs regardless of mission state — a Coder doing Step 1 research before
    create_mission lands still shows up if they're passing coder_id.
    """
    validate_os_hint(os_hint)

    mission = _active_mission(session)
    if mission is not None:
        mission.coder_last_seen = _utcnow()
        session.add(mission)
        session.commit()

    if coder_id is None:
        return

    # v1.13: per-Coder heartbeat. Resolve project_id from the current request's
    # ContextVar (NOT from the mission, since a Coder may be active before any
    # mission exists).
    pid = _project_id(session)
    if pid is None:
        return  # request landed on a project that doesn't exist — nothing to do

    now = _utcnow()
    existing = session.get(CoderHeartbeat, (pid, coder_id))
    if existing is not None:
        # Throttle: skip the write if we bumped within the interval. Coerce
        # naive datetimes (SQLite) to aware before comparing against `now`.
        last = existing.last_seen
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        if (now - last).total_seconds() < HEARTBEAT_MIN_INTERVAL_SECONDS:
            # CoderHeartbeat throttled — but AgentPresence still bumps per call
            # (its own table, lazy-promotion semantics). See _touch_presence below.
            _touch_presence(session, agent_key=coder_id, role="coder", project_id=pid)
            return
        existing.last_seen = now
        if os_hint is not None:
            existing.os_hint = os_hint
        session.add(existing)
    else:
        session.add(CoderHeartbeat(
            project_id=pid, coder_id=coder_id, last_seen=now, os_hint=os_hint,
            tenant_id=current_tenant(),
        ))
    session.commit()
    # Mission A: keep AgentPresence's heartbeat fresh on every coder tool call.
    # Default state="working" if no row exists yet (the coder is calling a tool,
    # ergo alive + acting). Existing rows keep whatever state was last declared.
    _touch_presence(session, agent_key=coder_id, role="coder", project_id=pid)


# ---------- AgentPresence helpers (Mission A) ----------
#
# State semantics:
#   declared by set_my_state: idle / working / waiting_on_planner /
#                             waiting_on_coder / waiting_on_user / blocked
#   promoted by read-time helper: stale (heartbeat >5 min on a non-idle active
#                                 state), dead (heartbeat >30 min)
#
# _touch_presence: per-tool-call heartbeat bump. Inserts a row with
# default_state when missing; otherwise bumps last_heartbeat_at only. This
# function does NOT change `state` or `detail` — those only update via
# set_my_state. Safe to call inside the existing session.

VALID_DECLARED_STATES = frozenset({
    "idle", "working", "waiting_on_planner", "waiting_on_coder",
    "waiting_on_user", "blocked",
})
AGENT_PRESENCE_DETAIL_MAX = 200
AGENT_PRESENCE_EXPECTED_SECONDS_MAX = 86400  # 24h cap on declared deadlines
AGENT_PRESENCE_STALE_SECONDS = 5 * 60   # 5 min → stale
AGENT_PRESENCE_DEAD_SECONDS = 30 * 60   # 30 min → dead

PLANNER_AGENT_KEY = "planner"


def _validate_agent_key(value: str) -> Optional[str]:
    """Return None if `value` is a legal agent_key, else an error string. "planner"
    plus any SLUG_PATTERN-matching string (including "planner" itself since lowercase
    letters match) are accepted. Empty / non-string / shell-meta values rejected.
    """
    if not isinstance(value, str) or not value:
        return "agent_key must be a non-empty string"
    if not SLUG_PATTERN.fullmatch(value):
        return (
            "agent_key must match the slug regex (1-42 lowercase letters/digits "
            "with internal hyphens). Pass 'planner' for the hivemind or your "
            "normalized coder_id (same id you pass to wait_for_planner_message)."
        )
    return None


def _sanitize_presence_detail(value: Any) -> Optional[str]:
    """Clamp a detail string: trim, cap to AGENT_PRESENCE_DETAIL_MAX, strip
    control characters except \\n. Returns None on empty/None input."""
    if value is None:
        return None
    if not isinstance(value, str):
        return None
    cleaned = "".join(ch for ch in value if ch == "\n" or ord(ch) >= 32).strip()
    if not cleaned:
        return None
    return cleaned[:AGENT_PRESENCE_DETAIL_MAX]


def _touch_presence(
    session: Session,
    agent_key: str,
    role: str,
    project_id: Optional[str] = None,
    default_state: str = "working",
) -> None:
    """Heartbeat-bump AgentPresence for (tenant, project, agent_key). Inserts a
    row with state=default_state if none exists; otherwise just bumps
    last_heartbeat_at (state + detail + expected_done_at untouched). Per-tool-call
    granularity — no throttle — so a Planner can see exactly when the last MCP
    call landed. Safe to call from inside any existing session.
    """
    if project_id is None:
        project_id = _project_id(session)
    if project_id is None:
        return  # project doesn't exist in this tenant — nothing to do
    now = _utcnow()
    row = session.exec(
        select(AgentPresence).where(
            AgentPresence.project_id == project_id,
            AgentPresence.agent_key == agent_key,
        )
    ).first()
    if row is None:
        session.add(AgentPresence(
            tenant_id=current_tenant(),
            project_id=project_id,
            agent_key=agent_key,
            role=role,
            state=default_state,
            transitioned_at=now,
            last_heartbeat_at=now,
            source="declared",
        ))
    else:
        row.last_heartbeat_at = now
        # `role` may have been wrong on a legacy row — keep it correct.
        if row.role != role:
            row.role = role
        session.add(row)
    session.commit()


def _touch_planner_presence(session: Session) -> None:
    """Convenience for planner-side `_do_*` helpers — same as _touch_presence
    with agent_key=PLANNER_AGENT_KEY and role='planner'. Default state stays
    'working' (the planner just made an MCP call, ergo alive)."""
    _touch_presence(session, agent_key=PLANNER_AGENT_KEY, role="planner")


def _promote_presence_state(state: str, last_heartbeat_at: datetime, now: datetime) -> str:
    """Lazy server-side state promotion. Pure function — no DB write. Applied at
    read time in list_agent_states + _build_state_payload so we don't run a
    background sweep. SQLite stores naive datetimes; coerce before subtracting.
    """
    if last_heartbeat_at is None:
        return state
    last = last_heartbeat_at if last_heartbeat_at.tzinfo is not None else last_heartbeat_at.replace(tzinfo=timezone.utc)
    age = (now - last).total_seconds()
    if age >= AGENT_PRESENCE_DEAD_SECONDS:
        return "dead"
    # Non-idle declared state going quiet → stale. Idle stays idle (the agent
    # explicitly said they have nothing to do; "stale-idle" would be noise).
    if state != "idle" and state not in ("stale", "dead") and age >= AGENT_PRESENCE_STALE_SECONDS:
        return "stale"
    return state


def _presence_dict(row: AgentPresence, now: Optional[datetime] = None) -> dict[str, Any]:
    """Serialize an AgentPresence row with the read-time state promotion applied."""
    if now is None:
        now = _utcnow()
    effective_state = _promote_presence_state(row.state, row.last_heartbeat_at, now)
    last_hb = row.last_heartbeat_at
    if last_hb is not None and last_hb.tzinfo is None:
        last_hb_aware = last_hb.replace(tzinfo=timezone.utc)
    else:
        last_hb_aware = last_hb
    seconds_since_heartbeat = (
        int((now - last_hb_aware).total_seconds()) if last_hb_aware is not None else None
    )
    return {
        "agent_key": row.agent_key,
        "role": row.role,
        "state": effective_state,
        "declared_state": row.state,  # what the agent declared, pre-promotion
        "detail": row.detail,
        "expected_done_at": row.expected_done_at.isoformat() if row.expected_done_at else None,
        "transitioned_at": row.transitioned_at.isoformat() if row.transitioned_at else None,
        "last_heartbeat_at": row.last_heartbeat_at.isoformat() if row.last_heartbeat_at else None,
        "seconds_since_heartbeat": seconds_since_heartbeat,
        "source": row.source,
    }


def _do_set_my_state(
    state: str,
    detail: Optional[str] = None,
    expected_seconds: Optional[int] = None,
    agent_key: str = PLANNER_AGENT_KEY,
) -> dict[str, Any]:
    """Upsert AgentPresence for (tenant, project, agent_key) with the declared
    state. transitioned_at bumps only when `state` actually changes (detail /
    expected_seconds-only updates leave it alone so the UI's "X working for 45s"
    counter stays sensible). Heartbeat always bumps."""
    if state not in VALID_DECLARED_STATES:
        return {
            "error": (
                f"state must be one of {sorted(VALID_DECLARED_STATES)}; "
                "'stale' and 'dead' are server-side promotions and can't be declared."
            ),
        }
    err = _validate_agent_key(agent_key)
    if err:
        return {"error": err}
    role = "planner" if agent_key == PLANNER_AGENT_KEY else "coder"
    detail_clean = _sanitize_presence_detail(detail)
    expected_at: Optional[datetime] = None
    if expected_seconds is not None:
        try:
            secs = int(expected_seconds)
        except (TypeError, ValueError):
            return {"error": "expected_seconds must be a positive integer"}
        if secs <= 0:
            return {"error": "expected_seconds must be positive"}
        if secs > AGENT_PRESENCE_EXPECTED_SECONDS_MAX:
            return {"error": f"expected_seconds capped at {AGENT_PRESENCE_EXPECTED_SECONDS_MAX} (24h)"}
        expected_at = _utcnow() + timedelta(seconds=secs)
    with Session(get_engine()) as session:
        pid = _project_id(session)
        if pid is None:
            return {"error": f"project '{current_project()}' does not exist"}
        now = _utcnow()
        row = session.exec(
            select(AgentPresence).where(
                AgentPresence.project_id == pid,
                AgentPresence.agent_key == agent_key,
            )
        ).first()
        if row is None:
            row = AgentPresence(
                tenant_id=current_tenant(),
                project_id=pid,
                agent_key=agent_key,
                role=role,
                state=state,
                detail=detail_clean,
                expected_done_at=expected_at,
                transitioned_at=now,
                last_heartbeat_at=now,
                source="declared",
            )
        else:
            if row.state != state:
                row.transitioned_at = now
                row.state = state
            row.detail = detail_clean
            row.expected_done_at = expected_at
            row.last_heartbeat_at = now
            row.role = role  # keep correct if a legacy row had it wrong
            row.source = "declared"
        session.add(row)
        session.commit()
        session.refresh(row)
        return {
            "ok": True,
            "agent_key": row.agent_key,
            "role": row.role,
            "state": row.state,
            "detail": row.detail,
            "expected_done_at": row.expected_done_at.isoformat() if row.expected_done_at else None,
            "transitioned_at": row.transitioned_at.isoformat(),
            "last_heartbeat_at": row.last_heartbeat_at.isoformat(),
        }


# Mission B: source='observed' bucket. Desktop reports what it ACTUALLY sees
# (PTY alive + in_flight + stdout age) so cloud-side agents see the ground truth
# without any agent declaring. set_my_state stays untouched — its writes are
# source='declared'. _presence_dict applies the standard lazy promotion to BOTH
# sources uniformly; an observed row that goes silent ages into stale/dead the
# same way as any other (auto-recovery if the desktop dies / loses network).
OBSERVED_BUCKET_STATES = frozenset({"idle", "working", "dead"})
OBSERVED_REPLAY_WINDOW_SECONDS = 60  # observed_at must be within ±this of now


def _do_publish_observed_presence(agents_payload: list[Any]) -> dict[str, Any]:
    """Batch-upsert AgentPresence rows from desktop observation. tenant + project
    come from the request context (set by the existing middleware on the route's
    bearer). Each entry: {agent_key, state, detail, observed_at}. Per entry:
    validate; upsert with source='observed', last_heartbeat_at=server-now (NOT
    observed_at — the observation is fresh, but heartbeat is server-anchored),
    transitioned_at bumped only when state changes. Continues past per-entry
    errors (collected + returned alongside the applied count).
    """
    if not isinstance(agents_payload, list):
        return {"error": "agents must be a list"}
    now = _utcnow()
    results: list[dict[str, Any]] = []
    errors: list[str] = []
    BATCH_CAP = 500
    for entry in agents_payload[:BATCH_CAP]:
        if not isinstance(entry, dict):
            errors.append("agent entry must be an object")
            continue
        agent_key = (entry.get("agent_key") or "").strip()
        state = (entry.get("state") or "").strip()
        detail = entry.get("detail")
        observed_at_str = entry.get("observed_at")
        key_err = _validate_agent_key(agent_key)
        if key_err:
            errors.append(f"agent_key={agent_key!r}: {key_err}")
            continue
        if state not in OBSERVED_BUCKET_STATES:
            errors.append(
                f"agent_key={agent_key!r}: observed state must be one of "
                f"{sorted(OBSERVED_BUCKET_STATES)} (got {state!r})"
            )
            continue
        # Replay defense: observed_at within ±60s of server-now. Tolerate missing
        # (treat as now) so a desktop with bad clock can still publish.
        if observed_at_str:
            try:
                ts = datetime.fromisoformat(str(observed_at_str).replace("Z", "+00:00"))
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                if abs((now - ts).total_seconds()) > OBSERVED_REPLAY_WINDOW_SECONDS:
                    errors.append(f"agent_key={agent_key!r}: observed_at outside ±{OBSERVED_REPLAY_WINDOW_SECONDS}s")
                    continue
            except (ValueError, AttributeError, TypeError):
                errors.append(f"agent_key={agent_key!r}: invalid observed_at")
                continue
        results.append({
            "agent_key": agent_key,
            "state": state,
            "detail": _sanitize_presence_detail(detail),
            "role": "planner" if agent_key == PLANNER_AGENT_KEY else "coder",
        })
    if not results:
        return {"ok": False, "applied": 0, "errors": errors}
    with Session(get_engine()) as session:
        pid = _project_id(session)
        if pid is None:
            return {"error": f"project '{current_project()}' does not exist"}
        applied = 0
        for r in results:
            row = session.exec(
                select(AgentPresence).where(
                    AgentPresence.project_id == pid,
                    AgentPresence.agent_key == r["agent_key"],
                )
            ).first()
            if row is None:
                row = AgentPresence(
                    tenant_id=current_tenant(),
                    project_id=pid,
                    agent_key=r["agent_key"],
                    role=r["role"],
                    state=r["state"],
                    detail=r["detail"],
                    transitioned_at=now,
                    last_heartbeat_at=now,
                    source="observed",
                )
            else:
                if row.state != r["state"]:
                    row.transitioned_at = now
                    row.state = r["state"]
                row.detail = r["detail"]
                row.last_heartbeat_at = now
                row.role = r["role"]
                row.source = "observed"
                # expected_done_at: observation doesn't carry a deadline. PRESERVE
                # any existing value (a still-relevant declared deadline survives).
            session.add(row)
            applied += 1
        session.commit()
    return {"ok": True, "applied": applied, "errors": errors}


def _do_set_planner_status(text: Optional[str], expected_seconds: Optional[int] = None) -> dict[str, Any]:
    """Cloud-side Hivemind status banner. Thin alias on _do_set_my_state with
    state inferred from text presence: non-empty → working+detail; empty/None →
    idle+clear-detail. Same shape return as set_my_state."""
    cleaned = (text or "").strip()
    if cleaned:
        return _do_set_my_state("working", detail=cleaned,
                                expected_seconds=expected_seconds,
                                agent_key=PLANNER_AGENT_KEY)
    return _do_set_my_state("idle", detail=None,
                            expected_seconds=expected_seconds,
                            agent_key=PLANNER_AGENT_KEY)


def _do_list_agent_states(project_slug: Optional[str] = None) -> dict[str, Any]:
    """Return every AgentPresence row for (tenant, project). Applies the lazy
    state promotion at read time. NO heartbeat side-effect on this call — same
    "info read" semantics as get_active_mission()."""
    with Session(get_engine()) as session:
        pid = _project_id(session, slug=project_slug) if project_slug else _project_id(session)
        if pid is None:
            return {"agents": []}
        rows = session.exec(
            select(AgentPresence)
            .where(AgentPresence.project_id == pid)
            .order_by(AgentPresence.role.desc(), AgentPresence.agent_key)
        ).all()
        now = _utcnow()
        return {"agents": [_presence_dict(r, now=now) for r in rows]}


# ---------- Module-level write operations (v1.5) ----------
# Extracted from nested @mcp.tool functions so both the MCP wrappers AND the
# dashboard HTTP handlers can call them. Single source of truth. Zero closure
# dependencies on register_tools-scoped names (verified by reading every one);
# they only touch module-level models, serializers, and helpers.
#
# The @mcp.tool decorated wrappers inside register_tools delegate directly to
# these — two layers on purpose. Don't try to remove the wrappers; FastMCP needs
# the decorated function for tool registration.


# All _do_<name> functions END by calling dashboard_events.broadcast() on the
# happy path. Co-locating the SSE push with the state mutation means any caller
# (MCP tool wrapper, HTTP handler, future caller) gets the push for free — no
# duplicated-side-effect risk where a new callsite forgets to broadcast.


def _broadcast_for_mission(session: Session, mission_id: Optional[str]) -> Optional[str]:
    """Return the project slug for a mission_id so callers can broadcast on the
    right per-project SSE channel. Falls back to the request ContextVar when
    the mission is missing (deleted) or the FK chain breaks.
    """
    if mission_id is None:
        return current_project()
    m = session.get(Mission, mission_id)
    if m is None or m.project_id is None:
        return current_project()
    proj = session.get(Project, m.project_id)
    return proj.slug if proj is not None else current_project()


def _broadcast_for_project_id(session: Session, project_id: Optional[str]) -> Optional[str]:
    """Resolve a project_id (uuid) to its slug for SSE channel addressing."""
    if project_id is None:
        return current_project()
    proj = session.get(Project, project_id)
    return proj.slug if proj is not None else current_project()


def _do_answer_question(question_id: str, answer: str) -> dict[str, Any]:
    err = _validate_text(answer, "answer", MAX_TEXT_LEN)
    if err:
        return err
    with Session(get_engine()) as session:
        _touch_planner_presence(session)  # Mission A: planner heartbeat
        q = _tenant_get(session, Question, question_id)
        if q is None:
            return {"error": f"no question with id {question_id}"}
        if q.answer is not None:
            return {"error": "question already answered", "question": _question_dict(q)}
        q.answer = answer
        q.answered_at = _utcnow()
        session.add(q)
        session.commit()
        session.refresh(q)
        result = _question_dict(q)
        slug = _broadcast_for_mission(session, q.mission_id)
    dashboard_events.broadcast(slug)
    return result


def _do_respond_to_summary(summary_id: str, response: str) -> dict[str, Any]:
    err = _validate_text(response, "response", MAX_TEXT_LEN)
    if err:
        return err
    with Session(get_engine()) as session:
        _touch_planner_presence(session)  # Mission A: planner heartbeat
        s = _tenant_get(session, Summary, summary_id)
        if s is None:
            return {"error": f"no summary with id {summary_id}"}
        if s.response is not None:
            return {"error": "summary already responded to", "summary": _summary_dict(s)}
        s.response = response
        s.responded_at = _utcnow()
        session.add(s)
        session.commit()
        session.refresh(s)
        result = _summary_dict(s)
        slug = _broadcast_for_mission(session, s.mission_id)
    dashboard_events.broadcast(slug)
    return result


def _do_ack_message(message_id: str) -> dict[str, Any]:
    with Session(get_engine()) as session:
        m = _tenant_get(session, Message, message_id)
        if m is None:
            return {"error": f"no message with id {message_id}"}
        if m.delivered_at is not None:
            # Idempotent no-op ack — already broadcast when first acked, don't re-broadcast.
            return _message_dict(m)
        m.delivered_at = _utcnow()
        session.add(m)
        session.commit()
        session.refresh(m)
        result = _message_dict(m)
        # Broadcast on the message's project (which is intrinsic to the row) so
        # the dashboard subscribed to that project sees the ack live, even if
        # the request's ContextVar happens to be a different project (cross-
        # project ack is a degenerate but possible case).
        slug = _broadcast_for_project_id(session, m.project_id)
    dashboard_events.broadcast(slug)
    return result


def _do_send_to_coder(body: str, target_coder_id: Optional[str] = None) -> dict[str, Any]:
    """Insert a planner_to_coder message. v1.11: optional target_coder_id
    addresses a specific Coder. None = broadcast (every Coder's
    wait_for_planner_message sees it). A specific id = only the Coder calling
    wait_for_planner_message(coder_id=X) with X matching receives it.
    """
    err = _validate_text(body, "body", MAX_TEXT_LEN)
    if err:
        return err
    try:
        validate_coder_id(target_coder_id)
    except ValueError as e:
        return {"error": str(e)}
    with Session(get_engine()) as session:
        _touch_planner_presence(session)  # Mission A: planner heartbeat
        mission = _active_mission(session)
        if mission is None:
            return {"error": "no active mission — cannot send"}
        m = Message(
            mission_id=mission.id,
            project_id=mission.project_id,
            direction="planner_to_coder",
            body=body,
            target_coder_id=target_coder_id,
            tenant_id=current_tenant(),
        )
        session.add(m)
        session.commit()
        session.refresh(m)
        result = _message_dict(m)
        project_slug = current_project()
    dashboard_events.broadcast(project_slug)
    return result


def _do_send_to_user(body: str) -> dict[str, Any]:
    """v1.8: Planner → dashboard user. Mirrors _do_send_to_coder but does NOT
    require an active mission — the inbox channel is global wrt missions.

    v1.9: scoped to the current request's project. mission_id soft-set from
    active mission if present; project_id is always set (defaults to "default"
    via the ContextVar) so per-project inbox isolation holds.
    """
    err = _validate_text(body, "body", MAX_TEXT_LEN)
    if err:
        return err
    with Session(get_engine()) as session:
        _touch_planner_presence(session)  # Mission A: planner heartbeat
        pid = _project_id(session)
        if pid is None:
            return {"error": f"project '{current_project()}' does not exist"}
        mission = _active_mission(session)
        m = Message(
            mission_id=mission.id if mission is not None else None,
            project_id=pid,
            direction="planner_to_user",
            body=body,
            tenant_id=current_tenant(),
        )
        session.add(m)
        session.commit()
        session.refresh(m)
        result = _message_dict(m)
        project_slug = current_project()
    dashboard_events.broadcast(project_slug)
    return result


def _do_send_to_planner_from_user(body: str) -> dict[str, Any]:
    """v1.8: dashboard user → Planner inbox. v1.9: scoped to current project;
    returns an error if the project slug doesn't resolve (rather than silently
    orphaning the row with project_id=None).
    """
    err = _validate_text(body, "body", MAX_TEXT_LEN)
    if err:
        return err
    with Session(get_engine()) as session:
        pid = _project_id(session)
        if pid is None:
            return {"error": f"project '{current_project()}' does not exist"}
        mission = _active_mission(session)
        m = Message(
            mission_id=mission.id if mission is not None else None,
            project_id=pid,
            direction="user_to_planner",
            body=body,
            tenant_id=current_tenant(),
        )
        session.add(m)
        session.commit()
        session.refresh(m)
        result = _message_dict(m)
        project_slug = current_project()
    dashboard_events.broadcast(project_slug)
    return result


def _do_create_mission(
    name: str,
    spec: str,
    project_slug: Optional[str] = None,
) -> dict[str, Any]:
    """Insert a new mission, superseding any prior active one IN THE SAME PROJECT.

    v1.9: scoped to the current request's project. Two projects can each have
    their own active mission simultaneously — they don't supersede each other.
    Atomic via the one_active_mission_per_project partial unique index +
    IntegrityError retry-once.

    v1.16: optional project_slug guards against cross-connector misrouting.
    The morning of 2026-05-26 a Hivemind silently superseded another
    project's active mission because it created on the wrong project URL.
    Passing project_slug=<expected> makes that class of bug hard-fail.
    """
    scope_err = validate_project_scope(project_slug)
    if scope_err:
        return scope_err
    gate_err = _check_plan_gate()
    if gate_err:
        return gate_err
    err = _validate_text(name, "name", MAX_NAME_LEN) or _validate_text(spec, "spec", MAX_SPEC_LEN)
    if err:
        return err
    for attempt in range(2):
        try:
            with Session(get_engine()) as session:
                _touch_planner_presence(session)  # Mission A: planner heartbeat
                pid = _project_id(session)
                if pid is None:
                    return {"error": f"project '{current_project()}' does not exist"}
                current = _active_mission(session)
                if current:
                    current.status = "superseded"
                    session.add(current)
                mission = Mission(name=name, spec=spec, status="active", project_id=pid, tenant_id=current_tenant())
                session.add(mission)
                # v2.x: the FIRST mission on a project seeds the durable foundation
                # mission (north-star goal). It is never superseded — a fresh-context
                # Planner can always recover the project's purpose. Refine later via
                # set_foundation_mission.
                proj = session.get(Project, pid)
                if proj is not None and not proj.foundation_name:
                    proj.foundation_name = name
                    proj.foundation_spec = spec
                    proj.foundation_set_at = _utcnow()
                    session.add(proj)
                session.commit()
                session.refresh(mission)
                result = _mission_dict(mission)
                project_slug = current_project()
            dashboard_events.broadcast(project_slug)
            return result
        except IntegrityError:
            if attempt == 0:
                continue
            return {
                "error": (
                    "create_mission contention: another concurrent creator beat us "
                    "twice in a row. The active mission belongs to someone else right "
                    "now — call get_active_mission to see it, then create_mission again "
                    "if you still want to supersede."
                )
            }


def _do_ask_planner(
    question: str,
    coder_id: Optional[str] = None,
    os_hint: Optional[str] = None,
    project_slug: Optional[str] = None,
) -> dict[str, Any]:
    """Insert a Question against the active mission. Does NOT block — the wait
    loop is MCP-protocol-specific and stays in the @mcp.tool ask_planner wrapper.
    Returns the question dict (with the new question_id) on success.

    v1.9: scoped to current project via _active_mission().
    v1.11: optional coder_id identifies which Coder asked, surfaced to the
    Hivemind via _question_dict. None = legacy single-Coder mode.
    v1.15: optional os_hint persists on the CoderHeartbeat row so the
    Connected Coders dashboard panel renders an OS icon next to this Coder.
    v1.16: optional project_slug. When supplied, server validates it matches
    the URL's actual project slug; mismatch returns a scope-error and the
    write is REJECTED. Closes the cross-connector misrouting bug where an
    agent picks the wrong MCP connector and silently corrupts another
    project's state.
    """
    scope_err = validate_project_scope(project_slug)
    if scope_err:
        return scope_err
    gate_err = _check_plan_gate()
    if gate_err:
        return gate_err
    err = _validate_text(question, "question", MAX_TEXT_LEN)
    if err:
        return err
    try:
        validate_coder_id(coder_id)
        validate_os_hint(os_hint)
    except ValueError as e:
        return {"error": str(e)}
    with Session(get_engine()) as session:
        _touch_coder(session, coder_id=coder_id, os_hint=os_hint)
        mission = _active_mission(session)
        if mission is None:
            return {"error": "no active mission — cannot ask"}
        q = Question(mission_id=mission.id, body=question, coder_id=coder_id, tenant_id=current_tenant())
        session.add(q)
        session.commit()
        session.refresh(q)
        result = _question_dict(q)
        slug = current_project()
    dashboard_events.broadcast(slug)
    return result


def _do_submit_progress(
    summary: str,
    coder_id: Optional[str] = None,
    os_hint: Optional[str] = None,
    project_slug: Optional[str] = None,
) -> dict[str, Any]:
    """Insert a Summary against the active mission. v1.9: scoped to current project.
    v1.11: optional coder_id identifies which Coder submitted.
    v1.15: optional os_hint persists on CoderHeartbeat for dashboard OS icon.
    v1.16: optional project_slug guards against cross-connector misrouting.
    """
    scope_err = validate_project_scope(project_slug)
    if scope_err:
        return scope_err
    gate_err = _check_plan_gate(count_report=True)
    if gate_err:
        return gate_err
    err = _validate_text(summary, "summary", MAX_TEXT_LEN)
    if err:
        return err
    try:
        validate_coder_id(coder_id)
        validate_os_hint(os_hint)
    except ValueError as e:
        return {"error": str(e)}
    with Session(get_engine()) as session:
        _touch_coder(session, coder_id=coder_id, os_hint=os_hint)
        mission = _active_mission(session)
        if mission is None:
            return {"error": "no active mission — cannot submit progress"}
        s = Summary(mission_id=mission.id, body=summary, coder_id=coder_id, tenant_id=current_tenant())
        session.add(s)
        session.commit()
        session.refresh(s)
        result = _summary_dict(s)
        slug = current_project()
    dashboard_events.broadcast(slug)
    return result


def _do_set_foundation(name: str, spec: str) -> dict[str, Any]:
    """Designate/refine the current project's foundation mission. Tenant+project
    scoped via _project_id. Not trial-gated (it's project meta, not mission work)."""
    err = _validate_text(name, "name", MAX_NAME_LEN) or _validate_text(spec, "spec", MAX_SPEC_LEN)
    if err:
        return err
    with Session(get_engine()) as session:
        _touch_planner_presence(session)  # Mission A: planner heartbeat
        pid = _project_id(session)
        if pid is None:
            return {"error": f"project '{current_project()}' does not exist"}
        proj = session.get(Project, pid)
        proj.foundation_name = name
        proj.foundation_spec = spec
        proj.foundation_set_at = _utcnow()
        session.add(proj)
        session.commit()
        session.refresh(proj)
        return _foundation_dict(proj)


def _do_mark_mission_done() -> dict[str, Any]:
    """v1.9: scoped to current project's active mission."""
    with Session(get_engine()) as session:
        _touch_planner_presence(session)  # Mission A: planner heartbeat
        mission = _active_mission(session)
        if mission is None:
            return {"error": "no active mission"}
        mission.status = "done"
        mission.done_at = _utcnow()
        session.add(mission)
        session.commit()
        session.refresh(mission)
        result = _mission_dict(mission)
        slug = current_project()
    dashboard_events.broadcast(slug)
    return result


def register_tools(mcp, settings: Settings) -> None:
    """Register every AgentsHive tool with the given FastMCP instance."""

    poll_interval = settings.poll_interval_seconds
    block_timeout = settings.tool_block_timeout_seconds

    # ---------- Long-poll helpers (v1.2 Feature 4 — DRY up 6 near-identical wait loops) ----------
    #
    # Two helpers, not one. The wait sites split into two patterns that don't combine cleanly:
    #   Pattern A — wait on a SPECIFIC row by id, return on terminal state, surface parent
    #               mission's status if it left "active" mid-wait. Async state machine.
    #               Used by _wait_for_question and _wait_for_summary.
    #   Pattern B — wait on the OLDEST matching row for the active mission, return when one
    #               appears. No lifecycle branch (the row IS for active mission by query
    #               construction). Pure pull-from-queue. Optional on_hit side-effect lets
    #               message tools increment redelivery_count without auto-acking.
    #               Used by wait_for_next_question, wait_for_next_summary,
    #               wait_for_coder_message, wait_for_planner_message.
    #
    # A previous draft tried a single helper with lifecycle_check=None — confusing conditional
    # paths defeated the abstraction. Two helpers, each does one thing.

    def _wait_specific(
        row_id,
        model_cls,
        id_key,
        is_terminal,
        terminal_status,
        to_dict,
        pending_msg,
    ):
        deadline = time.monotonic() + block_timeout
        while True:
            with Session(get_engine()) as session:
                row = _tenant_get(session, model_cls, row_id)
                if row is None:
                    return {"error": f"no {id_key.replace('_id', '')} with id {row_id}"}
                if is_terminal(row):
                    return {"status": terminal_status, **to_dict(row)}
                mission = _tenant_get(session, Mission, row.mission_id)
                if mission is not None and mission.status != "active":
                    return {
                        "status": mission.status,
                        id_key: row_id,
                        "mission_id": row.mission_id,
                        "message": (
                            "Your mission is no longer active — fetch_mission to get the new "
                            "spec and decide whether to restart."
                            if mission.status == "superseded"
                            else "Your mission is marked done — stop work."
                        ),
                    }
            if time.monotonic() >= deadline:
                return {"status": "pending", id_key: row_id, "message": pending_msg}
            time.sleep(poll_interval)

    def _wait_for_active(
        query_fn,
        to_dict,
        pending_msg,
        block_for,
        on_hit_mutate=None,
    ):
        deadline = time.monotonic() + block_for
        while True:
            with Session(get_engine()) as session:
                mission = _active_mission(session)
                if mission is not None:
                    row = query_fn(session, mission)
                    if row is not None:
                        if on_hit_mutate is not None:
                            on_hit_mutate(row, session)
                        return to_dict(row)
            if time.monotonic() >= deadline:
                return {"status": "pending", "message": pending_msg}
            time.sleep(poll_interval)

    def _wait_global(
        query_fn,
        to_dict,
        pending_msg,
        block_for,
        on_hit_mutate=None,
    ):
        """v1.8: Pattern B variant that does NOT require an active mission.

        Used by the dashboard inbox channel (wait_for_user_message). Identical
        shape to _wait_for_active except the query_fn takes only a session — no
        mission gate. Kept as a sibling rather than overloading _wait_for_active
        with a "skip mission check" flag because explicit beats clever in
        protocol code (validated repeatedly across v1.1/v1.2/v1.3).
        """
        deadline = time.monotonic() + block_for
        while True:
            with Session(get_engine()) as session:
                row = query_fn(session)
                if row is not None:
                    if on_hit_mutate is not None:
                        on_hit_mutate(row, session)
                    return to_dict(row)
            if time.monotonic() >= deadline:
                return {"status": "pending", "message": pending_msg}
            time.sleep(poll_interval)

    # ---------- Planner-side tools ----------

    @mcp.tool
    def create_mission(
        name: str,
        spec: str,
        project_slug: Optional[str] = None,
    ) -> dict[str, Any]:
        """Start a new AgentsHive mission. The Coder will fetch this spec and begin building.

        Any previously-active mission is marked 'superseded' — there is only one active
        mission at a time. Call this once you and the human have locked the spec.

        Args:
            name: short label for the mission (e.g., "Build the invoice exporter").
                Must be non-empty, max {MAX_NAME_LEN} characters.
            spec: the full natural-language specification the Coder should implement.
                Must be non-empty, max {MAX_SPEC_LEN // 1024} KB.
            project_slug: (v1.16+) optional safety check — the project slug you THINK
                this connection is routed to. Server validates against the actual ?project=
                URL slug; mismatch returns a hard error and the create is REFUSED. Strongly
                recommended when you have multiple AgentsHive MCP connectors loaded — call
                get_project_info() first to discover the slug, then pass it here to prove
                you're mutating the project you intend.
        """
        return _do_create_mission(name, spec, project_slug=project_slug)

    @mcp.tool
    def set_my_state(
        state: str,
        detail: Optional[str] = None,
        expected_seconds: Optional[int] = None,
        agent_key: str = PLANNER_AGENT_KEY,
    ) -> dict[str, Any]:
        """Declare YOUR current state so other agents + the operator can see what
        you're doing. Mission A foundation for the "you guys keep freezing"
        complaint — previously there was no first-class concept of agent state.

        agent_key:  default "planner" for the hivemind. CODERS MUST pass their
                    normalized coder_id (same id used for wait_for_planner_message).
        state:      one of idle | working | waiting_on_planner | waiting_on_coder
                    | waiting_on_user | blocked. "stale"/"dead" are server-side
                    promotions and rejected here.
        detail:     short free-text up to 200 chars — what you're doing
                    (e.g. "deploying server", "ask_planner: which endpoint shape?").
        expected_seconds: optional declared deadline. If set, expected_done_at =
                    now + expected_seconds. The UI uses this to surface "(~N s
                    remaining)" or "(over by N s)" so a stale "deploying ~120s"
                    claim becomes visible.

        Recommended calls (from the spec): before any long shell wait
        (railway up / npm install / tagging), declare working+detail+expected; after
        the work lands, set state="idle". Heartbeat bumps automatically on every
        other MCP call you make.
        """
        return _do_set_my_state(state, detail=detail, expected_seconds=expected_seconds, agent_key=agent_key)

    @mcp.tool
    def set_planner_status(text: Optional[str] = None, expected_seconds: Optional[int] = None) -> dict[str, Any]:
        """Cloud-side Hivemind status banner — for the planner connector that has
        no PTY for the desktop to observe. Thin alias on set_my_state:
          - text non-empty → state="working", detail=text
          - text empty/None → state="idle", clear detail
          - expected_seconds works the same as set_my_state

        Use this when you (the Planner) commit to a long action where the operator's
        desktop can't observe you (deploys, tag pushes, manual review). CODERS should
        use set_my_state(agent_key=<your coder_id>) instead — Mission B's desktop
        observer publishes their state automatically.

        Source for this write is "declared" — it surfaces under the planner's avatar
        when no fresher observation exists for agent_key="planner" (cloud planner
        has no observer, so this is the canonical signal for that agent)."""
        return _do_set_planner_status(text, expected_seconds=expected_seconds)

    @mcp.tool
    def list_agent_states(project_slug: Optional[str] = None) -> dict[str, Any]:
        """Show the current declared state of every agent in this project. Apply
        before sending: if the target coder is in {stale, dead}, prefer notifying
        the user via send_to_user instead of firing into a void.

        Returns {agents: [{agent_key, role, state, declared_state, detail,
        expected_done_at, transitioned_at, last_heartbeat_at, seconds_since_heartbeat,
        source}, ...]}. State is the EFFECTIVE state (lazy-promoted to stale after
        5 min of heartbeat silence on a non-idle state, dead after 30 min);
        declared_state is what the agent last actually claimed."""
        return _do_list_agent_states(project_slug=project_slug)

    @mcp.tool
    def get_active_mission(coder_id: Optional[str] = None) -> dict[str, Any]:
        """Return the currently active mission (spec + status), or None if none is active.

        v1.13: optional coder_id bumps the per-Coder heartbeat — useful when a
        Coder is polling for a mission to appear (no mission exists yet, so
        they can't ask_planner) and wants to show up in the Connected Coders
        panel as "alive and waiting".

        Without coder_id: NO heartbeat side effect, preserving pre-v1.13
        Planner-side behavior (v1.1 F3.b: list/get tools don't bump
        coder_last_seen). Only adding coder_id opts into the per-Coder
        heartbeat trail.
        """
        try:
            validate_coder_id(coder_id)
        except ValueError as e:
            return {"error": str(e)}
        with Session(get_engine()) as session:
            if coder_id is not None:
                _touch_coder(session, coder_id=coder_id)
            mission = _active_mission(session)
            pid = _project_id(session)
            foundation = _foundation_dict(session.get(Project, pid)) if pid else None
            if mission:
                result = _mission_dict(mission)
                result["foundation"] = foundation
                return result
            return {"mission": None, "foundation": foundation}

    @mcp.tool
    def list_pending_questions() -> list[dict[str, Any]]:
        """List every question the Coder has asked that you have not yet answered.

        Returns questions for the currently-active mission, oldest first.

        Transport note: list returns are wrapped by FastMCP's structured_content layer
        under a "result" key in the MCP message envelope. Most clients unwrap this
        automatically; if yours doesn't, look for {"result": [...]}. Prefer
        wait_for_next_question for a push-style loop that returns one item at a time.
        """
        with Session(get_engine()) as session:
            mission = _active_mission(session)
            if not mission:
                return []
            rows = session.exec(
                select(Question)
                .where(Question.mission_id == mission.id, Question.answer.is_(None))
                .order_by(Question.created_at)
            ).all()
            return [_question_dict(q) for q in rows]

    @mcp.tool
    def answer_question(question_id: str, answer: str) -> dict[str, Any]:
        """Answer a pending question from the Coder. The Coder will receive this and resume."""
        return _do_answer_question(question_id, answer)

    @mcp.tool
    def list_pending_summaries() -> list[dict[str, Any]]:
        """List every progress summary the Coder has submitted that you have not yet responded to.

        Transport note: list returns are wrapped by FastMCP's structured_content layer
        under a "result" key in the MCP message envelope. Most clients unwrap this
        automatically. Prefer wait_for_next_summary for a push-style loop.
        """
        with Session(get_engine()) as session:
            mission = _active_mission(session)
            if not mission:
                return []
            rows = session.exec(
                select(Summary)
                .where(Summary.mission_id == mission.id, Summary.response.is_(None))
                .order_by(Summary.created_at)
            ).all()
            return [_summary_dict(s) for s in rows]

    @mcp.tool
    def respond_to_summary(summary_id: str, response: str) -> dict[str, Any]:
        """Respond to a Coder progress summary. Use this to give direction, request changes, or say 'continue'.

        To mark the entire mission as finished, call mark_mission_done — not this.
        """
        return _do_respond_to_summary(summary_id, response)

    @mcp.tool
    def wait_for_next_question(timeout_seconds: Optional[float] = None) -> dict[str, Any]:
        """Block until any unanswered question exists for the currently-active mission.

        Use this instead of polling list_pending_questions in a loop. Mirrors the
        Coder-side ask_planner blocking semantics from the Planner's side: call
        once, the server blocks until a real item arrives.

        On hit: returns the single matching question (same shape as one entry from
        list_pending_questions). Pass `question_id` directly to answer_question.
        Returns the OLDEST unanswered question — answer in arrival order.

        On timeout: returns {status: "pending", message: ...}. The MCP transport
        will time out the call before the configured server-side timeout in most
        clients; just call wait_for_next_question again — there is no question_id
        to track because we are waiting on "whatever shows up next," not a
        specific one.

        Supersede behavior: if the active mission changes mid-wait (someone
        called create_mission), the new active mission's pending items become
        eligible. You wait for whoever's active, never for a specific mission.

        Args:
            timeout_seconds: optional override for how long the server blocks before
                returning "pending". Falls back to TOOL_BLOCK_TIMEOUT_SECONDS.
        """
        return _wait_for_active(
            lambda session, mission: session.exec(
                select(Question)
                .where(Question.mission_id == mission.id, Question.answer.is_(None))
                .order_by(Question.created_at)
            ).first(),
            _question_dict,
            "no questions yet — call wait_for_next_question again to keep waiting",
            timeout_seconds if timeout_seconds is not None else block_timeout,
        )

    @mcp.tool
    def wait_for_next_summary(timeout_seconds: Optional[float] = None) -> dict[str, Any]:
        """Block until any progress summary awaiting your response exists for the active mission.

        The summary-side companion to wait_for_next_question. Use instead of
        polling list_pending_summaries.

        On hit: returns the single oldest unresponded summary; pass summary_id
        directly to respond_to_summary.

        On timeout: returns {status: "pending", message: ...}. Call again to
        keep waiting.

        Supersede: same as wait_for_next_question — if the active mission
        changes mid-wait, the new active mission's pending summaries become
        eligible.

        Args:
            timeout_seconds: optional override for the server-side block.
                Falls back to TOOL_BLOCK_TIMEOUT_SECONDS.
        """
        return _wait_for_active(
            lambda session, mission: session.exec(
                select(Summary)
                .where(Summary.mission_id == mission.id, Summary.response.is_(None))
                .order_by(Summary.created_at)
            ).first(),
            _summary_dict,
            "no summaries yet — call wait_for_next_summary again to keep waiting",
            timeout_seconds if timeout_seconds is not None else block_timeout,
        )

    @mcp.tool
    def send_to_coder(
        body: str,
        target_coder_id: Optional[str] = None,
        project_slug: Optional[str] = None,
    ) -> dict[str, Any]:
        """Planner-side: send a free-form message TO the Coder. Fire-and-forget.

        Use this for casual "fyi…" / "while you're at it…" / "I noticed X" updates that
        don't need a structured response. The Coder reads via wait_for_planner_message().
        For structured Q&A or progress review, use answer_question / respond_to_summary
        as before — this is the chat-style channel, not a replacement.

        Inserts a Message addressed to the Coder against the currently-active mission.
        Returns the message_id immediately; does NOT block.

        v1.11: optional target_coder_id addresses a specific Coder by id.
        - target_coder_id=None (default): broadcast — every Coder that calls
          wait_for_planner_message receives it, including legacy Coders that
          don't declare a coder_id.
        - target_coder_id="A": targeted — only the Coder calling
          wait_for_planner_message(coder_id="A") receives it. Other identified
          Coders and legacy Coders (coder_id=None) do NOT see this message.

        Use targeted sends when N Coders are working the same mission and the
        message is relevant to only one (e.g., "Coder-server, switch to
        Postgres"). Use the default broadcast for general announcements.
        """
        scope_err = validate_project_scope(project_slug)
        if scope_err:
            return scope_err
        return _do_send_to_coder(body, target_coder_id=target_coder_id)

    @mcp.tool
    def wait_for_coder_message(timeout_seconds: Optional[float] = None) -> dict[str, Any]:
        """Planner-side: block until an unacked Coder→Planner message exists for the active mission.

        AT-LEAST-ONCE SEMANTICS (v1.2): returns the OLDEST unacked message but does NOT
        stamp delivered_at. Until you call ack_message(message_id), subsequent calls to
        this tool keep returning the same row (with redelivery_count incrementing each
        time). Reader pattern: wait → process → ack. If you crash before ack, you'll see
        the row again on next call — exactly the safety property you want.

        On timeout: {status: "pending", message: ...}. Call again to keep waiting.

        Args:
            timeout_seconds: optional override for the server-side block. Falls back to
                TOOL_BLOCK_TIMEOUT_SECONDS.
        """
        def _bump(m, session):
            m.redelivery_count = (m.redelivery_count or 0) + 1
            session.add(m)
            session.commit()
            session.refresh(m)
        return _wait_for_active(
            lambda session, mission: session.exec(
                select(Message)
                .where(
                    Message.mission_id == mission.id,
                    Message.direction == "coder_to_planner",
                    Message.delivered_at.is_(None),
                )
                .order_by(Message.created_at)
            ).first(),
            _message_dict,
            "no messages yet — call wait_for_coder_message again to keep waiting",
            timeout_seconds if timeout_seconds is not None else block_timeout,
            on_hit_mutate=_bump,
        )

    @mcp.tool
    def send_to_planner(
        body: str,
        coder_id: Optional[str] = None,
        os_hint: Optional[str] = None,
        project_slug: Optional[str] = None,
    ) -> dict[str, Any]:
        """Coder-side: send a free-form message TO the Planner. Fire-and-forget.

        Use this for "fyi…" / "I made a small tangential decision" / "here's an
        observation about AgentsHive itself" updates that don't warrant a full
        submit_progress checkpoint. The Planner reads via wait_for_coder_message().

        Inserts a Message addressed to the Planner against the active mission. Also
        bumps the Coder heartbeat. Returns the message_id immediately; does NOT block.

        v1.11: optional coder_id self-identifies the Coder. Surfaced to the
        Hivemind on _message_dict so they can attribute the note. None = legacy
        single-Coder mode (no attribution).
        v1.15: optional os_hint ("windows" | "macos" | "linux") persists on the
        CoderHeartbeat row for the dashboard's Connected Coders OS icon.
        """
        scope_err = validate_project_scope(project_slug)
        if scope_err:
            return scope_err
        err = _validate_text(body, "body", MAX_TEXT_LEN)
        if err:
            return err
        try:
            validate_coder_id(coder_id)
            validate_os_hint(os_hint)
        except ValueError as e:
            return {"error": str(e)}
        with Session(get_engine()) as session:
            _touch_coder(session, coder_id=coder_id, os_hint=os_hint)
            mission = _active_mission(session)
            if mission is None:
                return {"error": "no active mission — cannot send"}
            m = Message(
                mission_id=mission.id,
                project_id=mission.project_id,
                direction="coder_to_planner",
                body=body,
                coder_id=coder_id,
                tenant_id=current_tenant(),
            )
            session.add(m)
            session.commit()
            session.refresh(m)
            result = _message_dict(m)
            slug = current_project()
        dashboard_events.broadcast(slug)
        return result

    @mcp.tool
    def wait_for_planner_message(
        timeout_seconds: Optional[float] = None,
        coder_id: Optional[str] = None,
        since: Optional[str] = None,
    ) -> dict[str, Any]:
        """Coder-side: block until an unacked Planner→Coder message exists for the active mission.

        AT-LEAST-ONCE SEMANTICS (v1.2): returns the OLDEST unacked message but does NOT
        stamp delivered_at. Until you call ack_message(message_id), subsequent calls keep
        returning the same row (with redelivery_count incrementing). Reader pattern:
        wait → process → ack. If you crash before ack, you'll see the row again next call.

        Also bumps the Coder heartbeat (single touch on entry, not per poll iteration).

        On timeout: {status: "pending", message: ...}. Call again to keep waiting.

        v1.11 — targeting filter for multi-Coder coordination. Pass `coder_id` to
        self-identify. The four delivery paths:
          - sender target=None + coder_id=None or any value  → DELIVERED (broadcast)
          - sender target="A" + coder_id="A"                  → DELIVERED (targeted match)
          - sender target="A" + coder_id="B"                  → NOT DELIVERED (other Coder's)
          - sender target="A" + coder_id=None (legacy)        → NOT DELIVERED (legacy
            Coders never claim a target identity, so they only see broadcasts)

        v1.13 — `since` parameter for crash-resume. Pass either an ISO 8601 timestamp
        or a message_id (32-char lowercase hex) and only messages created strictly
        after that point are eligible. Both at-least-once semantics and the coder_id
        routing matrix still apply on top. Malformed / future / non-existent `since`
        is silently ignored (returns as if since wasn't passed).

        Args:
            timeout_seconds: optional override for the server-side block. Falls back to
                TOOL_BLOCK_TIMEOUT_SECONDS.
            coder_id: optional self-identifier. Validated against the same slug regex
                as project slugs. None preserves pre-v1.11 behavior (broadcast-only).
            since: optional resume marker — ISO 8601 timestamp OR a message_id (32-hex).
                Server auto-detects format. None preserves pre-v1.13 behavior (no filter).
        """
        try:
            validate_coder_id(coder_id)
        except ValueError as e:
            return {"error": str(e)}

        # Resolve since once, before entering the poll loop. None = no filter.
        # Bad input (malformed, unknown message_id, future timestamp, older than
        # earliest row) silently falls back to "no filter" per the brief.
        since_at = _resolve_since(since)

        with Session(get_engine()) as session:
            _touch_coder(session, coder_id=coder_id)
        def _bump(m, session):
            m.redelivery_count = (m.redelivery_count or 0) + 1
            session.add(m)
            session.commit()
            session.refresh(m)

        def _query(session, mission):
            from sqlalchemy import or_
            stmt = (
                select(Message)
                .where(
                    Message.mission_id == mission.id,
                    Message.direction == "planner_to_coder",
                    Message.delivered_at.is_(None),
                )
            )
            if coder_id is None:
                # Legacy / broadcast-only mode: only target_coder_id IS NULL rows.
                stmt = stmt.where(Message.target_coder_id.is_(None))
            else:
                # Identified Coder: broadcasts + messages targeted at me.
                stmt = stmt.where(
                    or_(Message.target_coder_id.is_(None), Message.target_coder_id == coder_id)
                )
            if since_at is not None:
                stmt = stmt.where(Message.created_at > since_at)
            return session.exec(stmt.order_by(Message.created_at)).first()

        return _wait_for_active(
            _query,
            _message_dict,
            "no messages yet — call wait_for_planner_message again to keep waiting",
            timeout_seconds if timeout_seconds is not None else block_timeout,
            on_hit_mutate=_bump,
        )

    # ---------- v1.8 Inbox channel — Planner ↔ dashboard user ----------
    #
    # Two new Message direction values, both global (no active mission required):
    #   - user_to_planner — sent from the dashboard chat composer; received by
    #     the Planner via wait_for_user_message
    #   - planner_to_user — sent from this Planner-side tool send_to_user;
    #     rendered live in the dashboard chat panel via SSE
    #
    # ack_message is direction-agnostic and handles both directions transparently
    # (verified by reading _do_ack_message — no direction filter). The dashboard
    # auto-acks planner_to_user rows on render so the Planner doesn't see them
    # bounce back as unacked. user_to_planner rows MUST be acked by the Planner
    # AFTER processing (v1.2 at-least-once contract) — never auto-acked.

    @mcp.tool
    def send_to_user(body: str) -> dict[str, Any]:
        """Planner-side: send a free-form message TO the dashboard user. Fire-and-forget.

        v1.8 global inbox: this is the back-channel to the human running the
        dashboard. Use it for async replies to things the user typed in the
        dashboard chat panel, or to drop a note for them to see whenever they
        next open the dashboard. The user reads via the dashboard chat panel
        (rendered live via SSE); they do not need to "fetch" anything.

        Does NOT require an active mission — the inbox is global. If a mission
        is active when you call this, the message is tagged with its id so the
        UI can render mission boundaries; if not, mission_id stays None.

        Returns the message_id immediately; does NOT block.
        """
        return _do_send_to_user(body)

    @mcp.tool
    def wait_for_user_message(timeout_seconds: Optional[float] = None) -> dict[str, Any]:
        """Planner-side: block until an unacked dashboard-user message exists in the inbox.

        AT-LEAST-ONCE SEMANTICS (v1.2): returns the OLDEST unacked user_to_planner
        message but does NOT stamp delivered_at. Until you call ack_message(message_id),
        subsequent calls keep returning the same row (with redelivery_count incrementing).
        Reader pattern: wait → process → reply via send_to_user (optional) → ack.

        v1.8: NOT scoped to active mission — the inbox is global wrt missions.
        v1.9: NOW scoped to the current request's project. A Planner connected
        to project A's MCP URL only sees project A's inbox; switching projects
        means a different MCP URL (Q4: one MCP entry per project per Coder/
        Planner session).

        On timeout: {status: "pending", message: ...}. Call again to keep waiting.

        Args:
            timeout_seconds: optional override for the server-side block. Falls back to
                TOOL_BLOCK_TIMEOUT_SECONDS.
        """
        def _bump(m, session):
            m.redelivery_count = (m.redelivery_count or 0) + 1
            session.add(m)
            session.commit()
            session.refresh(m)
        return _wait_global(
            lambda session: (
                session.exec(
                    select(Message)
                    .where(
                        Message.direction == "user_to_planner",
                        Message.delivered_at.is_(None),
                        Message.project_id == _project_id(session),
                    )
                    .order_by(Message.created_at)
                ).first()
                if _project_id(session) is not None
                else None
            ),
            _message_dict,
            "no inbox messages yet — call wait_for_user_message again to keep waiting",
            timeout_seconds if timeout_seconds is not None else block_timeout,
            on_hit_mutate=_bump,
        )

    @mcp.tool
    def list_inbox_history(limit: int = 50) -> list[dict[str, Any]]:
        """Planner-side: snapshot of the recent chat history with the dashboard user.

        Returns up to `limit` (default 50) most-recent inbox messages — BOTH directions
        (user_to_planner and planner_to_user) — ordered oldest to newest. Use this on
        session return to see what the user wrote while you were away, or to scroll back
        through earlier turns of a long chat.

        Does not affect delivered_at — read-only. Pair with wait_for_user_message for
        new arrivals and ack_message after processing.

        Transport note: list returns are wrapped by FastMCP's structured_content layer
        under a "result" key in the MCP message envelope. Most clients unwrap this
        automatically.
        """
        with Session(get_engine()) as session:
            pid = _project_id(session)
            if pid is None:
                return []
            rows = session.exec(
                select(Message)
                .where(
                    Message.direction.in_(("user_to_planner", "planner_to_user")),
                    Message.project_id == pid,
                )
                .order_by(Message.created_at.desc())
                .limit(max(1, min(limit, 500)))
            ).all()
            return [_message_dict(m) for m in reversed(rows)]

    @mcp.tool
    def ack_message(message_id: str) -> dict[str, Any]:
        """Acknowledge receipt of a message returned by wait_for_coder_message or wait_for_planner_message.

        Idempotent: calling ack on an already-acked message is a no-op that returns the
        same message dict. Both Planner and Coder use this — the symmetric design avoids
        a "who should ack this?" handshake on top of the role split.

        Reader pattern (v1.2 at-least-once):
            msg = wait_for_*_message(...)
            ... process msg ...
            ack_message(msg["message_id"])

        If you skip the ack, the next wait_for_*_message call returns the same row with
        redelivery_count incremented. If you crashed between wait and ack, that's the
        feature — the message lives on for the next reader instead of vanishing into
        the void of "delivered but never seen."

        Returns the full message dict with the (possibly newly stamped) delivered_at.
        """
        return _do_ack_message(message_id)

    # ---------- Symmetric meta tools (v1.3) ----------

    async def _list_tool_names() -> list[str]:
        tools = await mcp.list_tools()
        if isinstance(tools, list):
            return [t.name for t in tools]
        return list(tools.keys())

    @mcp.tool
    async def get_server_info() -> dict[str, Any]:
        """Return server metadata for catalog-drift detection. Pure — no side effects.

        Use this to detect whether your MCP client's cached view of AgentsHive is stale
        relative to what the server actually exposes today. Three fields:

          server_version: semver string of the running AgentsHive build (e.g. "1.3.0").
              A change vs. your last observed value means the server was redeployed.

          tools_catalog_hash: short fingerprint (16 hex chars) of the registered tool
              names. Stable across calls within a deploy; changes if any tool is added,
              removed, or renamed. If THIS differs from your previous observation, your
              cached tool list is stale — call refresh_tool_catalog to ask the server
              to push a refresh signal, or manually disconnect/reconnect your MCP client.

          started_at: ISO timestamp of when this server process started. A change here
              also indicates a redeploy (the server PID changed).

        Important: this tool does NOT trigger a tool-list refresh. It only reports the
        current state. Use refresh_tool_catalog when you want the server to emit a
        notifications/tools/list_changed event for spec-compliant clients.
        """
        names = await _list_tool_names()
        return {
            "server_version": AGENTSHIVE_VERSION,
            "tools_catalog_hash": _compute_tools_catalog_hash(names),
            "started_at": SERVER_STARTED_AT.isoformat(),
            # v1.12: project_slug surfaced so agents can sanity-check their MCP wiring
            # before calling create_mission. The morning 2026-05-26 incident -- two
            # Hiveminds colliding on the same project because nobody verified scope --
            # is what motivated this. Cheap inline addition; no extra round trip needed.
            "project_slug": current_project(),
        }

    @mcp.tool
    def get_foundation_mission() -> dict[str, Any]:
        """Return this project's FOUNDATION MISSION — its durable north-star goal.

        The foundation mission is set from the project's first mission (or refined
        via set_foundation_mission) and is NEVER superseded by the rotating active
        mission. Use it to re-ground on what the project is ultimately about —
        especially as a fresh Planner that has lost prior conversation context.

        Returns {name, spec, set_at}, or {foundation: None} if not set yet.
        """
        with Session(get_engine()) as session:
            pid = _project_id(session)
            if pid is None:
                return {"foundation": None}
            fd = _foundation_dict(session.get(Project, pid))
            return fd if fd is not None else {"foundation": None}

    @mcp.tool
    def set_foundation_mission(name: str, spec: str, project_slug: Optional[str] = None) -> dict[str, Any]:
        """Designate or refine this project's FOUNDATION MISSION (north-star goal).

        The first create_mission auto-seeds the foundation; call this to set it
        explicitly or refine it later. It persists separately from the active
        mission and is never superseded.

        v1.16: optional project_slug guards against cross-connector misrouting.
        """
        scope_err = validate_project_scope(project_slug)
        if scope_err:
            return scope_err
        return _do_set_foundation(name, spec)

    @mcp.tool
    def get_project_info() -> dict[str, Any]:
        """Return metadata about the current request's project context.

        v1.12: closes the project-scoping footgun that ate 3 hours of debugging on
        2026-05-26 (a Hivemind session was MCP-wired to the wrong ?project= slug
        and silently superseded another project's active mission).

        Every Hivemind/Coder session's FIRST action should be get_project_info()
        + verify the returned slug matches what the user asked you to drive. If it
        mismatches: STOP, send_to_user, do not call create_mission.

        Returns:
          slug: the project this MCP connection is scoped to (from ?project= URL param)
          name: human-readable display name
          description: optional free-form text from project creation
          created_at: ISO timestamp of project creation
          archived_at: ISO timestamp if archived, else None
          mission_count: total missions ever created on this project (any status)
          active_mission_id: the currently-active mission's id, or None if no mission is active
        """
        slug = current_project()
        with Session(get_engine()) as session:
            proj = session.exec(
                select(Project).where(Project.slug == slug, Project.tenant_id == current_tenant())
            ).first()
            if proj is None:
                # Should not happen — ProjectContextMiddleware returns 400 before any
                # tool sees an unknown slug. Defensive return for direct-invocation
                # tests / future code paths.
                return {
                    "slug": slug,
                    "name": None,
                    "description": None,
                    "created_at": None,
                    "archived_at": None,
                    "mission_count": 0,
                    "active_mission_id": None,
                    "error": f"project '{slug}' not found in database",
                }
            mission_count = len(session.exec(
                select(Mission.id).where(Mission.project_id == proj.id)
            ).all())
            active = session.exec(
                select(Mission).where(
                    Mission.project_id == proj.id,
                    Mission.status == "active",
                )
            ).first()
            return {
                "slug": proj.slug,
                "name": proj.name,
                "description": proj.description,
                "created_at": proj.created_at.isoformat(),
                "archived_at": proj.archived_at.isoformat() if proj.archived_at else None,
                "mission_count": mission_count,
                "active_mission_id": active.id if active else None,
                # v2.x: the durable north-star goal, always in reach for a fresh Planner.
                "foundation": _foundation_dict(proj),
            }

    @mcp.tool
    async def refresh_tool_catalog(ctx: Context) -> dict[str, Any]:
        """Ask the server to push a tools/list_changed notification to your session.

        Use this when get_server_info reports a tools_catalog_hash you haven't seen
        before — your cached tool list is stale and you want the server to nudge
        your client to re-list.

        Side effect: emits an MCP notifications/tools/list_changed event scoped to
        the calling session. Spec-compliant MCP clients respond by re-fetching the
        tool list automatically; you should see new tools in your toolbelt without
        any client-side action.

        Caveat: some MCP clients (notably Claude Code in current versions) cache the
        tool catalog aggressively across underlying HTTP reconnects and do NOT respect
        the notification. Those clients require a manual disconnect/reconnect — close
        and reopen the client app, or toggle the connector off and on in the UI. See
        the README troubleshooting section.

        Returns: {ok, tools_catalog_hash, message}. The hash matches what
        get_server_info would return; included so a caller can confirm what they
        refreshed to without a second round-trip.
        """
        names = await _list_tool_names()
        try:
            await ctx.send_notification(ToolListChangedNotification())
        except Exception as e:
            # Notification emission failing should not break the diagnostic — the hash
            # in the return value is still useful even if the push didn't go out.
            return {
                "ok": False,
                "tools_catalog_hash": _compute_tools_catalog_hash(names),
                "message": (
                    f"notification emission failed ({type(e).__name__}: {e}). "
                    "Manual client reconnect required to refresh the cache."
                ),
            }
        return {
            "ok": True,
            "tools_catalog_hash": _compute_tools_catalog_hash(names),
            "message": (
                "tools/list_changed notification emitted. Spec-compliant clients will "
                "re-list automatically. Aggressively-caching clients (e.g., Claude Code) "
                "need a manual disconnect/reconnect — see README."
            ),
        }

    @mcp.tool
    def mark_mission_done(project_slug: Optional[str] = None) -> dict[str, Any]:
        """Mark the active mission as done. The Coder will see this on its next is_mission_done() check and stop.

        v1.16: optional project_slug guards against cross-connector misrouting.
        If passed and mismatches the URL's actual project slug, the mark is REFUSED.
        """
        scope_err = validate_project_scope(project_slug)
        if scope_err:
            return scope_err
        return _do_mark_mission_done()

    # ---------- Coder-side tools ----------

    @mcp.tool
    def fetch_mission(coder_id: Optional[str] = None) -> dict[str, Any]:
        """Fetch the currently-active mission spec from AgentsHive. Call this first when starting work.

        Returns the mission's name, spec, status, and mission_id. If there is no active mission,
        the Planner has not created one yet — wait or stop.

        v1.13: optional coder_id bumps the per-Coder heartbeat so this Coder shows
        up in the Connected Coders dashboard panel even before they've asked a
        question or submitted a summary.
        """
        try:
            validate_coder_id(coder_id)
        except ValueError as e:
            return {"error": str(e)}
        with Session(get_engine()) as session:
            _touch_coder(session, coder_id=coder_id)
            mission = _active_mission(session)
            if mission is None:
                return {"mission": None, "message": "No active mission. The Planner has not started one yet."}
            return _mission_dict(mission)

    def _wait_for_question(question_id: str) -> dict[str, Any]:
        return _wait_specific(
            question_id,
            Question,
            "question_id",
            lambda q: q.answer is not None,
            "answered",
            _question_dict,
            (
                "Planner has not answered yet. Call wait_for_answer(question_id) "
                "to keep waiting — this is not an error, just a long-running operation."
            ),
        )

    @mcp.tool
    def ask_planner(
        question: str,
        coder_id: Optional[str] = None,
        os_hint: Optional[str] = None,
        project_slug: Optional[str] = None,
    ) -> dict[str, Any]:
        """Ask the Planner a question and wait for the answer.

        Use this whenever you would otherwise stop and ask the human. This is the entire
        point of AgentsHive — the Planner (running in Claude or Codex desktop/mobile) becomes
        your human substitute.

        Behavior: this call blocks until the Planner answers, up to an internal timeout
        (~4 minutes by default; controlled by TOOL_BLOCK_TIMEOUT_SECONDS). If the timeout
        is hit before an answer arrives, you get a {status: "pending", question_id: ...}
        response — call wait_for_answer(question_id) repeatedly until you get a real
        answer. Do NOT treat 'pending' as failure.

        v1.11: optional coder_id self-identifies the Coder so the Hivemind can
        attribute the question when multiple Coders work the same mission. None =
        legacy single-Coder mode.
        v1.15: optional os_hint ("windows" | "macos" | "linux") persists on the
        CoderHeartbeat row so the dashboard's Connected Coders panel renders an
        OS icon for this Coder. Useful when running Coders across devices.
        """
        # Insert via the module-level _do_ function so the dashboard SSE push fires.
        inserted = _do_ask_planner(
            question, coder_id=coder_id, os_hint=os_hint, project_slug=project_slug,
        )
        if "error" in inserted:
            return inserted
        return _wait_for_question(inserted["question_id"])

    @mcp.tool
    def wait_for_answer(question_id: str, coder_id: Optional[str] = None) -> dict[str, Any]:
        """Continue waiting for the Planner to answer a previously-asked question.

        Use this when ask_planner returned status="pending" (the MCP transport timed out
        before the Planner answered). Keep calling until you get status="answered".

        v1.13: optional coder_id bumps the per-Coder heartbeat.
        """
        try:
            validate_coder_id(coder_id)
        except ValueError as e:
            return {"error": str(e)}
        with Session(get_engine()) as session:
            _touch_coder(session, coder_id=coder_id)
        return _wait_for_question(question_id)

    def _wait_for_summary(summary_id: str) -> dict[str, Any]:
        return _wait_specific(
            summary_id,
            Summary,
            "summary_id",
            lambda s: s.response is not None,
            "responded",
            _summary_dict,
            (
                "Planner has not responded yet. Call wait_for_summary_response(summary_id) "
                "to keep waiting."
            ),
        )

    @mcp.tool
    def submit_progress(
        summary: str,
        coder_id: Optional[str] = None,
        os_hint: Optional[str] = None,
        project_slug: Optional[str] = None,
    ) -> dict[str, Any]:
        """Push a natural-language progress summary to the Planner and wait for their response.

        Call this at meaningful checkpoints (after each feature / milestone). Write in plain
        English — the Planner judges your work from this text, NOT from raw code or diffs.
        Be honest about what was done, what wasn't, and any decisions you made along the way.

        Behavior: blocks until the Planner responds. If the MCP transport times out first,
        you get status="pending" + summary_id — call wait_for_summary_response(summary_id)
        to keep waiting.

        v1.11: optional coder_id self-identifies the Coder. The Hivemind sees the
        coder_id on every summary so they can attribute the work in a multi-Coder
        run. None = legacy single-Coder mode.
        v1.15: optional os_hint ("windows" | "macos" | "linux") persists on the
        CoderHeartbeat row for the dashboard's Connected Coders OS icon.
        """
        # Insert via the module-level _do_ function so the dashboard SSE push fires.
        inserted = _do_submit_progress(
            summary, coder_id=coder_id, os_hint=os_hint, project_slug=project_slug,
        )
        if "error" in inserted:
            return inserted
        return _wait_for_summary(inserted["summary_id"])

    @mcp.tool
    def wait_for_summary_response(summary_id: str, coder_id: Optional[str] = None) -> dict[str, Any]:
        """Continue waiting for the Planner to respond to a previously-submitted summary.

        v1.13: optional coder_id bumps the per-Coder heartbeat.
        """
        try:
            validate_coder_id(coder_id)
        except ValueError as e:
            return {"error": str(e)}
        with Session(get_engine()) as session:
            _touch_coder(session, coder_id=coder_id)
        return _wait_for_summary(summary_id)

    @mcp.tool
    def is_mission_done(mission_id: Optional[str] = None, coder_id: Optional[str] = None) -> dict[str, Any]:
        """Check the status of a mission.

        Without an argument: backward-compatible behavior — reports on the latest applicable
        mission (active first, else most-recently-done). Useful as a simple "are we shipped?"
        check when the Coder only ever cares about the current top mission.

        With mission_id: report on that specific mission. Use this when you're holding a
        mission_id from an earlier fetch_mission / ask_planner / submit_progress and want
        to know whether the mission you're actually working on is active, superseded by a
        newer one, or done.

        Returns: {done: bool, status: "active"|"done"|"superseded"|None, mission: dict|None}
        - done is True ONLY when status == "done"
        - status carries the literal mission.status so the Coder can branch correctly
          (e.g., distinguish "Planner started a new mission, fetch_mission and restart"
          from "Planner shipped this one, stop")

        v1.13: optional coder_id bumps the per-Coder heartbeat. Useful for a
        Coder polling is_mission_done in a loop after mission completion — they
        stay visible in the Connected Coders panel until they stop polling.
        """
        try:
            validate_coder_id(coder_id)
        except ValueError as e:
            return {"error": str(e)}
        with Session(get_engine()) as session:
            _touch_coder(session, coder_id=coder_id)
            if mission_id is not None:
                m = _tenant_get(session, Mission, mission_id)
                if m is None:
                    return {
                        "done": False,
                        "status": None,
                        "mission": None,
                        "error": f"no mission with id {mission_id}",
                    }
                return {
                    "done": m.status == "done",
                    "status": m.status,
                    "mission": _mission_dict(m),
                }

            active = _active_mission(session)
            if active is not None:
                return {"done": False, "status": active.status, "mission": _mission_dict(active)}
            most_recent_done = session.exec(
                select(Mission)
                .where(Mission.status == "done", Mission.tenant_id == current_tenant())
                .order_by(Mission.created_at.desc())
            ).first()
            if most_recent_done is not None:
                return {"done": True, "status": "done", "mission": _mission_dict(most_recent_done)}
            return {"done": False, "status": None, "mission": None, "message": "No mission exists yet."}
