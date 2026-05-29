import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import JSON, Column, UniqueConstraint
from sqlalchemy.engine import Engine
from sqlmodel import Field, SQLModel, create_engine

from .config import Settings
from .tenant import LEGACY_TENANT


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _uuid() -> str:
    return uuid.uuid4().hex


# Mission status values: "active" | "done" | "superseded"
# - active: the one mission the Coder should be working on (per-project, v1.9+)
# - done: Planner called mark_mission_done — Coder should stop
# - superseded: a newer mission was created, this one was replaced before completion


class Project(SQLModel, table=True):
    """v1.9 namespace: missions, questions, summaries, messages, and SSE subscriptions
    are all scoped to a project. Coders bind to a project at the URL level
    (`?project=zecru-widget` on the MCP URL) and the request-time middleware sets
    the project ContextVar that every `_do_*` helper reads.

    v2.x: also tenant-scoped. `(tenant_id, slug)` is unique — the same slug can
    exist independently under different tenants — and `_project_id()` resolves by
    that pair, which is the single chokepoint that isolates every child query.
    """
    # v2.x: per-tenant slug uniqueness replaces the old global UNIQUE(slug). On a
    # fresh DB create_all builds this composite constraint; on the existing prod
    # DB the global unique is swapped to this by scripts/migrate_tenancy.py.
    __table_args__ = (UniqueConstraint("tenant_id", "slug", name="uq_project_tenant_slug"),)

    id: str = Field(default_factory=_uuid, primary_key=True)
    # v2.x: owning tenant. Supabase user id (sub) for real tenants, or LEGACY_TENANT
    # for pre-v2 / shared-key data. Indexed because resolution filters on it.
    tenant_id: str = Field(default=LEGACY_TENANT, index=True)
    # URL-facing identifier. Validated server-side (kebab-case, 1-42 chars). "default"
    # is reserved and created by _apply_inline_migrations; all other slugs come from
    # POST /api/dashboard/projects which calls project.validate_slug(). No longer
    # globally unique — unique only within a tenant (see __table_args__).
    slug: str = Field(index=True)
    name: str  # Display name shown in the dashboard switcher; user-editable.
    description: Optional[str] = None
    created_at: datetime = Field(default_factory=_utcnow)
    # Soft-archive (Planner Q3): hides from the switcher but preserves all child data.
    # Hard delete is v1.9.x.
    archived_at: Optional[datetime] = None
    # v2.x foundation mission: the project's durable north-star goal. Captured from
    # the FIRST mission created (or set/refined explicitly) and NEVER superseded —
    # so a fresh-context Planner can always recover what the project is ultimately
    # about. Stored on the project row (1:1, tenant-scoped) rather than as a Mission
    # so the rotating-active-mission supersede logic can't touch it.
    foundation_name: Optional[str] = None
    foundation_spec: Optional[str] = None
    foundation_set_at: Optional[datetime] = None


class Mission(SQLModel, table=True):
    id: str = Field(default_factory=_uuid, primary_key=True)
    # v2.x: denormalized owning tenant. Resolution already isolates project-rooted
    # queries; this column is the LIVE filter for by-id lookups (session.get(Mission,
    # id)) so a tenant can't touch another tenant's row by guessing an id. Nullable
    # because legacy rows are backfilled by _apply_inline_migrations; new writes set it.
    tenant_id: Optional[str] = Field(default=None, index=True)
    # v1.9: every mission belongs to exactly one project. NOT NULL — legacy rows
    # backfilled to "default" by _apply_inline_migrations.
    project_id: str = Field(foreign_key="project.id", index=True)
    name: str
    spec: str
    status: str = Field(default="active", index=True)
    created_at: datetime = Field(default_factory=_utcnow)
    done_at: Optional[datetime] = None
    # Updated by every Coder-side tool call (fetch_mission, ask_planner, submit_progress,
    # is_mission_done, the wait_for_* helpers, send_message when role=coder). Lets the
    # Planner tell whether the Coder is alive without an explicit ping protocol.
    coder_last_seen: Optional[datetime] = None


class Question(SQLModel, table=True):
    id: str = Field(default_factory=_uuid, primary_key=True)
    # v2.x: denormalized tenant — LIVE filter for by-id access (answer_question).
    tenant_id: Optional[str] = Field(default=None, index=True)
    mission_id: str = Field(foreign_key="mission.id", index=True)
    body: str
    answer: Optional[str] = None
    created_at: datetime = Field(default_factory=_utcnow)
    answered_at: Optional[datetime] = None
    # v1.11: optional per-Coder identity. None = legacy single-Coder mode.
    # Validated via project.validate_coder_id at the Coder-side tool entry point.
    coder_id: Optional[str] = Field(default=None)


class Summary(SQLModel, table=True):
    id: str = Field(default_factory=_uuid, primary_key=True)
    # v2.x: denormalized tenant — LIVE filter for by-id access (respond_to_summary).
    tenant_id: Optional[str] = Field(default=None, index=True)
    mission_id: str = Field(foreign_key="mission.id", index=True)
    body: str
    response: Optional[str] = None
    created_at: datetime = Field(default_factory=_utcnow)
    responded_at: Optional[datetime] = None
    # v1.11: see Question.coder_id.
    coder_id: Optional[str] = Field(default=None)


# Free-form bidirectional chat channel ADDITIVE to the structured ask/answer + submit/respond
# loops. Use for "hey also fyi…" updates that don't need a structured response.
# direction:        "planner_to_coder" | "coder_to_planner"
# delivered_at:     stamped when ack_message is called for this row (semantic shift from
#                   v1.1: previously meant "returned to a reader once," now means "the reader
#                   explicitly acknowledged receipt"). Column name kept for migration ease;
#                   it really means acked_at. v1.3 may rename.
# redelivery_count: incremented every time wait_for_*_message returns this row while still
#                   unacked. 0 means "first delivery"; any positive value means "someone (you
#                   or a predecessor) saw this and didn't ack." Diagnostic only — not a
#                   correctness guarantee, since a server-side increment may over-count if a
#                   client crashed mid-response.
class Message(SQLModel, table=True):
    id: str = Field(default_factory=_uuid, primary_key=True)
    # v1.8: mission_id is OPTIONAL — the "user_to_planner" / "planner_to_user"
    # inbox channel is global wrt mission. Mission-scoped sends (coder/planner
    # directions) still populate this field; inbox sends leave it NULL if there's
    # no active mission, OR set it to the active mission's id as a soft
    # "this happened during mission X" association.
    mission_id: Optional[str] = Field(default=None, foreign_key="mission.id", index=True)
    # v2.x: denormalized tenant — LIVE filter for by-id access (ack_message).
    tenant_id: Optional[str] = Field(default=None, index=True)
    # v1.9: every message belongs to exactly one project (including inbox messages).
    # Nullable because legacy pre-v1.9 rows existed before the column did; the
    # migration backfills to "default". New writes always set this.
    project_id: Optional[str] = Field(default=None, foreign_key="project.id", index=True)
    direction: str = Field(index=True)
    body: str
    created_at: datetime = Field(default_factory=_utcnow)
    delivered_at: Optional[datetime] = None
    redelivery_count: Optional[int] = Field(default=0)
    # v1.11: per-Coder identity. coder_id = sender (set on coder_to_planner /
    # planner_to_coder when the Coder declares an id). target_coder_id = recipient
    # filter on planner_to_coder messages: None = broadcast to every Coder; a
    # specific id = only the Coder calling wait_for_planner_message(coder_id=X)
    # with X matching receives it. Legacy Coders (coder_id=None on their wait
    # call) only see broadcast messages.
    coder_id: Optional[str] = Field(default=None)
    target_coder_id: Optional[str] = Field(default=None)
    # v2.x companion webapp relay. agent_key = the desktop agent.id this message is
    # for (web_to_agent) or from (agent_to_web). parent_id correlates an agent_to_web
    # response back to the originating web_to_agent so the webapp threads it. Both
    # nullable/additive; only set on the web_to_agent / agent_to_web directions.
    agent_key: Optional[str] = Field(default=None, index=True)
    parent_id: Optional[str] = Field(default=None, index=True)


# v1.13: per-Coder heartbeat for multi-Coder workflows. Lets the Connected
# Coders dashboard panel show a Coder as "alive" even when they haven't yet
# inserted a Question/Summary/Message — important for the realistic case where
# a Coder is mid-Step-1-research with no protocol output yet but is otherwise
# healthy. Updated by _touch_coder when the caller passes coder_id, throttled
# to at most one write per HEARTBEAT_MIN_INTERVAL_SECONDS per (project_id,
# coder_id) pair so a chatty Coder doesn't generate ~N writes/sec.
#
# Composite PK (project_id, coder_id) — a coder_id is unique within a project
# but two projects can each have a "coder-server" without colliding.
#
# TODO (v1.14+): no GC yet. Rows accumulate forever (~100B each — even at 1000
# distinct coders that's 100KB). A future cleanup pass would be UX-driven
# ("clear the panel"), not perf-driven. Add age-based delete when the panel
# UX starts feeling cluttered.
class CoderHeartbeat(SQLModel, table=True):
    project_id: str = Field(foreign_key="project.id", primary_key=True)
    coder_id: str = Field(primary_key=True)
    # v2.x: denormalized tenant (project_id already implies it, but kept for
    # consistency + any future by-id access). Nullable; backfilled by migration.
    tenant_id: Optional[str] = Field(default=None, index=True)
    last_seen: datetime = Field(default_factory=_utcnow)
    # v1.15: device hint ("windows" | "macos" | "linux"). Surfaces on the
    # Connected Coders dashboard panel as an OS icon so the user can tell at
    # a glance which device each Coder is running on (useful when running
    # Coders across Windows + Mac in one mission). Validated server-side
    # against a strict allow-list; None means "unknown / cloud Coder".
    os_hint: Optional[str] = Field(default=None)


# --- v2.x per-tenant account / billing / plan ----------------------------
#
# One row per tenant (Supabase sub, or LEGACY_TENANT). Holds the plan + the
# billing/trial counters Stripe (deferred) will later drive, plus the `banned`
# flag the admin panel toggles. Lazily created on first touch via
# get_or_create_tenant. The legacy tenant is seeded pro_unlimited so the
# transitional shared-key path (and our own dogfood coordination) is never
# trial-gated.

PLAN_FREE = "free"
PLAN_PRO = "pro"
PLAN_PRO_UNLIMITED = "pro_unlimited"
VALID_PLANS = frozenset({PLAN_FREE, PLAN_PRO, PLAN_PRO_UNLIMITED})


class Tenant(SQLModel, table=True):
    # tenant_id = Supabase user id (sub), or LEGACY_TENANT. Matches the tenant_id
    # stamped on every scoped row.
    tenant_id: str = Field(primary_key=True)
    plan: str = Field(default=PLAN_FREE, index=True)
    # P2 (deferred) billing fields — present so the gate + admin panel can read
    # them now and Stripe can populate them later without another migration.
    subscription_status: str = Field(default="none")
    trial_reports_used: int = Field(default=0)
    stripe_customer_id: Optional[str] = Field(default=None)
    stripe_subscription_id: Optional[str] = Field(default=None)
    # Admin-toggled. A banned tenant's token is rejected at auth time.
    banned: bool = Field(default=False, index=True)
    # v2.x Cloud Sync (opt-in paid add-on; Stripe billing DEFERRED). Admin-assignable
    # entitlement; pro_unlimited resolves to True via cloud_sync_enabled(). When this
    # is False (and not pro_unlimited) the tenant's conversation transcripts never
    # leave the user's machine — privacy-first default.
    cloud_sync: bool = Field(default=False)
    email: Optional[str] = Field(default=None)  # cached for admin display; source of truth is Supabase
    created_at: datetime = Field(default_factory=_utcnow)


# --- v2.x Cloud Sync (opt-in): tenant-scoped conversation transcript store ---
#
# When a tenant ENABLES Cloud Sync (entitlement-gated — see cloud_sync_enabled),
# the desktop pushes its per-agent conversation transcripts here for cross-device
# + companion-webapp access. PRIVACY-FIRST: with the entitlement off / opted out,
# NOTHING is written here — transcripts stay on the user's machine.
#
# Strictly tenant-scoped. tenant_id is denormalized onto SyncedMessage as the LIVE
# enforced filter that closes by-id access (same pattern as the coordination
# tables). Identity/upsert key is a CLIENT-GENERATED per-message UUID (msg_uuid),
# NOT the array index — so local re-index / clear / reorder can never overwrite the
# wrong server row. `idx` is render ORDER only.

class SyncedConversation(SQLModel, table=True):
    __table_args__ = (
        UniqueConstraint("tenant_id", "project_slug", "agent_id", name="uq_syncedconvo_tenant_project_agent"),
    )
    id: str = Field(default_factory=_uuid, primary_key=True)
    tenant_id: str = Field(index=True)
    project_slug: str = Field(index=True)
    agent_id: str = Field(index=True)
    label: Optional[str] = None
    role: Optional[str] = None
    cli: Optional[str] = None
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow, index=True)


class SyncedMessage(SQLModel, table=True):
    __table_args__ = (
        UniqueConstraint("tenant_id", "msg_uuid", name="uq_syncedmsg_tenant_uuid"),
    )
    id: str = Field(default_factory=_uuid, primary_key=True)
    # Denormalized owning tenant — the LIVE filter on by-id access (IDOR-closing).
    tenant_id: str = Field(index=True)
    # Client-generated stable id; the upsert identity together with tenant_id.
    msg_uuid: str = Field(index=True)
    project_slug: str = Field(index=True)
    agent_id: str = Field(index=True)
    idx: int = Field(default=0)  # render ORDER within the conversation (not identity)
    role: str = Field(default="assistant")
    text: str = Field(default="")
    tool_calls: Optional[list] = Field(default=None, sa_column=Column(JSON))
    tokens: Optional[dict] = Field(default=None, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow, index=True)


# v2.x companion-webapp presence: the desktop upserts one row per local agent so
# the webapp can list a tenant's agents and show desktop online/offline (via
# last_seen freshness). Tenant-scoped; agent_key = the desktop agent.id.
class WebAgentPresence(SQLModel, table=True):
    tenant_id: str = Field(primary_key=True)
    agent_key: str = Field(primary_key=True)
    project_id: Optional[str] = Field(default=None, index=True)
    project_slug: Optional[str] = Field(default=None)
    label: Optional[str] = None
    role: Optional[str] = None
    cli: Optional[str] = None
    status: Optional[str] = None
    last_seen: datetime = Field(default_factory=_utcnow)


# --- v1.7 OAuth 2.1 storage ---------------------------------------------
#
# Four tables back the AgentsHiveOAuthProvider. Access tokens and refresh
# tokens are stored as SHA256 hex digests so a DB leak does not surrender
# the live bearer values; the raw token string only exists in transit and
# in the requesting client. Authorization codes are also hashed for the
# same reason, even though their 10-minute TTL limits exposure.
#
# JSON columns (redirect_uris, scopes, grant_types, response_types) use
# SQLAlchemy's generic JSON type and so work transparently on both SQLite
# and Postgres without a dialect-specific column class.


class OAuthClient(SQLModel, table=True):
    # client_id is the public identifier issued at registration time.
    client_id: str = Field(primary_key=True)
    # v2.x: tenant that registered the dynamic client. Legacy/public DCR uses
    # LEGACY_TENANT; authenticated DCR can cap clients per real tenant.
    tenant_id: Optional[str] = Field(default=None, index=True)
    # client_secret is None for public clients (PKCE-only, no secret).
    client_secret: Optional[str] = None
    client_name: Optional[str] = None
    redirect_uris: list = Field(default_factory=list, sa_column=Column(JSON))
    grant_types: list = Field(default_factory=list, sa_column=Column(JSON))
    response_types: list = Field(default_factory=list, sa_column=Column(JSON))
    scope: Optional[str] = None
    token_endpoint_auth_method: Optional[str] = None
    created_at: datetime = Field(default_factory=_utcnow)
    # Bumped on every successful /authorize and /token use — drives the
    # LRU eviction policy when the registered-clients cap is exceeded.
    last_used_at: datetime = Field(default_factory=_utcnow)


class OAuthAuthorizationCode(SQLModel, table=True):
    # SHA256 hex of the raw code value — never the code itself.
    code_hash: str = Field(primary_key=True)
    client_id: str = Field(index=True)
    scopes: list = Field(default_factory=list, sa_column=Column(JSON))
    expires_at: int  # unix seconds
    code_challenge: str
    redirect_uri: str
    redirect_uri_provided_explicitly: bool = True
    resource: Optional[str] = None
    used: bool = False  # single-use enforcement
    created_at: datetime = Field(default_factory=_utcnow)
    # v2.x: the Supabase tenant (sub) of the user who consented. Carried onto the
    # minted access token so the Claude-app MCP connector is tenant-bound.
    tenant_id: Optional[str] = Field(default=None)


class OAuthAccessToken(SQLModel, table=True):
    token_hash: str = Field(primary_key=True)
    client_id: str = Field(index=True)
    scopes: list = Field(default_factory=list, sa_column=Column(JSON))
    expires_at: int  # unix seconds
    resource: Optional[str] = None
    revoked: bool = False
    created_at: datetime = Field(default_factory=_utcnow)
    # v2.x: tenant (Supabase sub) this token acts as. load_access_token surfaces it
    # so the request's tenant context can be bound from the OAuth token.
    tenant_id: Optional[str] = Field(default=None)


class OAuthRefreshToken(SQLModel, table=True):
    token_hash: str = Field(primary_key=True)
    client_id: str = Field(index=True)
    scopes: list = Field(default_factory=list, sa_column=Column(JSON))
    expires_at: int  # unix seconds
    revoked: bool = False
    created_at: datetime = Field(default_factory=_utcnow)
    # v2.x: tenant carried through refresh rotation so a refreshed access token
    # stays bound to the same tenant.
    tenant_id: Optional[str] = Field(default=None)


_engine: Optional[Engine] = None


def init_engine(settings: Settings) -> Engine:
    global _engine
    connect_args = {}
    if settings.database_url.startswith("sqlite"):
        connect_args = {"check_same_thread": False}
    _engine = create_engine(settings.database_url, connect_args=connect_args, pool_pre_ping=True)
    SQLModel.metadata.create_all(_engine)
    _apply_inline_migrations(_engine)
    return _engine


def _apply_inline_migrations(engine: Engine) -> None:
    """Lightweight idempotent migrations for the handful of additive columns
    that have been added after the initial schema. SQLModel.metadata.create_all
    will create *missing tables* but never alter an existing one, so we issue
    ALTER TABLE statements directly. Each block is wrapped to swallow the
    "duplicate column" error so the function is safe to run on every startup
    against any version of the DB.
    """
    from sqlalchemy import inspect, text

    inspector = inspect(engine)
    if "mission" not in inspector.get_table_names():
        return  # nothing to migrate on a fresh DB

    existing_cols = {col["name"] for col in inspector.get_columns("mission")}

    mission_additive = {
        # name -> ALTER TABLE column-definition fragment (works on both SQLite and Postgres)
        "coder_last_seen": "TIMESTAMP NULL",
    }

    with engine.begin() as conn:
        for col_name, col_def in mission_additive.items():
            if col_name in existing_cols:
                continue
            conn.execute(text(f"ALTER TABLE mission ADD COLUMN {col_name} {col_def}"))

    if "message" in inspector.get_table_names():
        msg_cols_full = inspector.get_columns("message")
        msg_cols = {col["name"] for col in msg_cols_full}
        message_additive = {
            "redelivery_count": "INTEGER DEFAULT 0",
        }
        with engine.begin() as conn:
            for col_name, col_def in message_additive.items():
                if col_name in msg_cols:
                    continue
                conn.execute(text(f"ALTER TABLE message ADD COLUMN {col_name} {col_def}"))

        # v1.8: relax NOT NULL on message.mission_id so the global inbox can store
        # mission_id=None. Two cases:
        #   - Postgres: a single ALTER COLUMN ... DROP NOT NULL, idempotent.
        #   - SQLite: NOT NULL on an existing column requires a table-rebuild
        #     dance we deliberately skipped (Planner Q5: prod is Postgres, dev is
        #     throwaway). Emit a clear warning so the dev knows to rm the DB file
        #     instead of getting a cryptic OperationalError on first send_to_user.
        mission_id_col = next((c for c in msg_cols_full if c["name"] == "mission_id"), None)
        is_nullable = bool(mission_id_col and mission_id_col.get("nullable", True))
        if not is_nullable:
            dialect_name = engine.dialect.name
            if dialect_name == "postgresql":
                with engine.begin() as conn:
                    conn.execute(text("ALTER TABLE message ALTER COLUMN mission_id DROP NOT NULL"))
            else:
                import logging
                logging.getLogger(__name__).warning(
                    "Pre-v1.8 %s DB detected: message.mission_id is still NOT NULL. "
                    "The new global inbox (user_to_planner / planner_to_user) requires "
                    "a nullable mission_id. Delete your local DB file and restart to "
                    "regenerate the schema. Production Postgres handles this via inline "
                    "ALTER; SQLite intentionally does not.",
                    dialect_name,
                )

    # v1.9: multi-project namespacing.
    #
    # The order is load-bearing:
    #   1. Ensure the "default" Project row exists (FK target for the backfill).
    #   2. Add project_id columns to mission + message; backfill to "default".
    #   3. Drop the v1.2 `one_active_mission` partial unique index (single-active-
    #      mission-globally) and replace it with a per-project partial unique index.
    #   4. Defensive cleanup of multi-active-per-project rows (shouldn't happen
    #      with the supersede-and-retry logic, but the CREATE INDEX would fail
    #      otherwise so be defensive).
    #
    # The migration runs during _apply_inline_migrations which is called from
    # init_engine BEFORE uvicorn starts accepting requests — so there's no race
    # with concurrent create_mission. We still wrap the index swap in a single
    # transaction so a startup crash mid-migration leaves a consistent state.

    from .project import DEFAULT_PROJECT_SLUG

    # Step 1: seed the "default" project row if missing. Uses raw SQL so we don't
    # have to import Project here (would create a circular import at module load).
    with engine.begin() as conn:
        existing_default = conn.execute(
            text("SELECT id FROM project WHERE slug = :slug"),
            {"slug": DEFAULT_PROJECT_SLUG},
        ).fetchone()
        if existing_default is None:
            from uuid import uuid4
            conn.execute(
                text(
                    "INSERT INTO project (id, tenant_id, slug, name, description, created_at, archived_at) "
                    "VALUES (:id, :tenant_id, :slug, :name, :description, :created_at, NULL)"
                ),
                {
                    "id": uuid4().hex,
                    "tenant_id": LEGACY_TENANT,
                    "slug": DEFAULT_PROJECT_SLUG,
                    "name": "Default",
                    "description": "Legacy / unscoped traffic. Pre-v1.9 missions and messages live here.",
                    "created_at": datetime.now(timezone.utc),
                },
            )
        default_project_id = conn.execute(
            text("SELECT id FROM project WHERE slug = :slug"),
            {"slug": DEFAULT_PROJECT_SLUG},
        ).fetchone()[0]

    # Step 2: add project_id to mission + message if missing, backfill to default.
    mission_cols_full = inspector.get_columns("mission")
    mission_cols = {c["name"] for c in mission_cols_full}
    dialect_name = engine.dialect.name
    if "project_id" not in mission_cols:
        # default_project_id is a uuid hex (32 hex chars from uuid4().hex) so it's
        # safe to interpolate directly. Bind parameters don't work in DDL on
        # Postgres ("could not determine data type of parameter $1") so the
        # literal-string path is the only one that works for both dialects.
        assert all(c in "0123456789abcdef" for c in default_project_id), \
            f"unsafe project id for SQL interpolation: {default_project_id!r}"
        with engine.begin() as conn:
            if dialect_name == "postgresql":
                conn.execute(text(
                    f"ALTER TABLE mission ADD COLUMN project_id TEXT NOT NULL "
                    f"DEFAULT '{default_project_id}' REFERENCES project(id)"
                ))
                conn.execute(text("ALTER TABLE mission ALTER COLUMN project_id DROP DEFAULT"))
                conn.execute(text("CREATE INDEX IF NOT EXISTS ix_mission_project_id ON mission (project_id)"))
            else:
                # SQLite: ALTER TABLE ADD COLUMN supports NOT NULL with a DEFAULT.
                # We can't easily DROP DEFAULT on SQLite but the ORM always
                # supplies project_id explicitly, so the DEFAULT just sits unused.
                conn.execute(text(
                    f"ALTER TABLE mission ADD COLUMN project_id TEXT NOT NULL DEFAULT '{default_project_id}'"
                ))
                conn.execute(text("CREATE INDEX IF NOT EXISTS ix_mission_project_id ON mission (project_id)"))

    if "message" in inspector.get_table_names():
        msg_cols_v19 = {c["name"] for c in inspector.get_columns("message")}
        if "project_id" not in msg_cols_v19:
            with engine.begin() as conn:
                # Message.project_id is nullable (v1.8 mission_id pattern) so no DEFAULT
                # dance needed. Backfill explicitly so legacy inbox rows show in the
                # default project's dashboard inbox.
                if dialect_name == "postgresql":
                    conn.execute(text("ALTER TABLE message ADD COLUMN project_id TEXT REFERENCES project(id)"))
                else:
                    conn.execute(text("ALTER TABLE message ADD COLUMN project_id TEXT"))
                conn.execute(text("CREATE INDEX IF NOT EXISTS ix_message_project_id ON message (project_id)"))
                conn.execute(
                    text("UPDATE message SET project_id = :pid WHERE project_id IS NULL"),
                    {"pid": default_project_id},
                )

    # Step 3+4: swap the partial unique index from "global single active" to
    # "single active per project". DROP + CREATE in one transaction; the defensive
    # cleanup runs first to keep CREATE INDEX from failing on pre-existing
    # multi-active rows (could happen if a project has stale actives from before
    # the supersede-and-retry logic landed).
    with engine.begin() as conn:
        conn.execute(text("DROP INDEX IF EXISTS one_active_mission"))
        # Defensive per-project cleanup: keep newest active per project, demote rest
        rows = conn.execute(
            text(
                "SELECT id, project_id FROM mission WHERE status='active' "
                "ORDER BY project_id, created_at DESC"
            )
        ).fetchall()
        seen_projects: set[str] = set()
        for mid, pid in rows:
            if pid in seen_projects:
                conn.execute(
                    text("UPDATE mission SET status='superseded' WHERE id = :id"),
                    {"id": mid},
                )
            else:
                seen_projects.add(pid)
        conn.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS one_active_mission_per_project "
                "ON mission (project_id, status) WHERE status = 'active'"
            )
        )

    # v1.11: per-Coder identity columns.
    #
    # Three additive nullable columns (question.coder_id, summary.coder_id,
    # message.coder_id) plus message.target_coder_id. All Optional[str], nullable,
    # no DEFAULT — pre-v1.11 rows surface as NULL which means "legacy single-Coder
    # mode" through the rest of the protocol.
    #
    # Partial index on (mission_id, coder_id) for the eventual "filter pending
    # items by Coder" query path. Postgres takes the partial WHERE clause
    # natively; SQLite versions before 3.8.0 (March 2014) silently ignore it, so
    # we fall back to a non-partial index there — same shape, slightly larger.
    v111_inspector = inspect(engine)
    v111_table_specs = [
        ("question", ["coder_id"]),
        ("summary", ["coder_id"]),
        ("message", ["coder_id", "target_coder_id"]),
    ]
    with engine.begin() as conn:
        for table_name, col_names in v111_table_specs:
            if table_name not in v111_inspector.get_table_names():
                continue
            existing = {c["name"] for c in v111_inspector.get_columns(table_name)}
            for col_name in col_names:
                if col_name in existing:
                    continue
                conn.execute(text(f"ALTER TABLE {table_name} ADD COLUMN {col_name} TEXT"))

    with engine.begin() as conn:
        dialect_name = engine.dialect.name
        for table_name, _cols in v111_table_specs:
            if table_name not in v111_inspector.get_table_names():
                continue
            index_name = f"ix_{table_name}_mission_coder"
            if dialect_name == "postgresql":
                conn.execute(text(
                    f"CREATE INDEX IF NOT EXISTS {index_name} "
                    f"ON {table_name}(mission_id, coder_id) WHERE coder_id IS NOT NULL"
                ))
            else:
                conn.execute(text(
                    f"CREATE INDEX IF NOT EXISTS {index_name} "
                    f"ON {table_name}(mission_id, coder_id)"
                ))

    # v1.13: CoderHeartbeat table + supporting index.
    #
    # SQLModel.metadata.create_all already creates the table when the engine is
    # fresh — this block is belt-and-suspenders for already-running v1.11/v1.12
    # databases that pre-date the CoderHeartbeat declaration. CREATE TABLE IF
    # NOT EXISTS is safe on both SQLite and Postgres.
    #
    # The (project_id, last_seen) index serves the Connected Coders panel
    # query: "WHERE project_id = ? AND last_seen > now - ttl". Cheap; one row
    # per coder_id per project so the index stays tiny.
    v113_inspector = inspect(engine)
    if "coderheartbeat" not in v113_inspector.get_table_names():
        with engine.begin() as conn:
            conn.execute(text(
                "CREATE TABLE IF NOT EXISTS coderheartbeat ("
                "project_id TEXT NOT NULL REFERENCES project(id), "
                "coder_id TEXT NOT NULL, "
                "last_seen TIMESTAMP NOT NULL, "
                "PRIMARY KEY (project_id, coder_id)"
                ")"
            ))
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_coderheartbeat_project_lastseen "
            "ON coderheartbeat(project_id, last_seen)"
        ))

    # v1.15: os_hint column on coderheartbeat. Idempotent ALTER TABLE - inspect
    # current columns and only ALTER if missing. Same pattern as v1.11's coder_id
    # column additions. Nullable so legacy rows (pre-v1.15) stay valid.
    v115_inspector = inspect(engine)
    if "coderheartbeat" in v115_inspector.get_table_names():
        existing_cols = {c["name"] for c in v115_inspector.get_columns("coderheartbeat")}
        if "os_hint" not in existing_cols:
            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE coderheartbeat ADD COLUMN os_hint TEXT"))

    # v2.x: multi-tenancy. ADDITIVE + REVERSIBLE only — add nullable tenant_id
    # columns and backfill existing rows to LEGACY_TENANT so the legacy shared-key
    # path keeps seeing exactly today's data. The RISKY part (swapping the global
    # UNIQUE(slug) to UNIQUE(tenant_id, slug)) is deliberately NOT done here; it is
    # staged in scripts/migrate_tenancy.py for a supervised production run. On a
    # fresh DB the composite unique comes from create_all (Project.__table_args__),
    # so dev/test multi-tenancy works without touching the staged script.
    v2_inspector = inspect(engine)
    v2_tables = [
        "project", "mission", "question", "summary", "message",
        "coderheartbeat", "oauthclient", "oauthaccesstoken", "oauthauthorizationcode",
        "oauthrefreshtoken",
    ]
    with engine.begin() as conn:
        for tbl in v2_tables:
            if tbl not in v2_inspector.get_table_names():
                continue
            cols = {c["name"] for c in v2_inspector.get_columns(tbl)}
            if "tenant_id" not in cols:
                conn.execute(text(f"ALTER TABLE {tbl} ADD COLUMN tenant_id TEXT"))
    with engine.begin() as conn:
        for tbl in v2_tables:
            if tbl not in v2_inspector.get_table_names():
                continue
            conn.execute(
                text(f"UPDATE {tbl} SET tenant_id = :t WHERE tenant_id IS NULL"),
                {"t": LEGACY_TENANT},
            )
        # Resolution + by-id-filter index. Additive and safe on both dialects.
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_project_tenant ON project(tenant_id)"))

    # v2.x companion-webapp: additive nullable columns on message (agent_key,
    # parent_id) for the web_to_agent / agent_to_web relay. WebAgentPresence table
    # is created by create_all. Safe + idempotent.
    v2w_inspector = inspect(engine)
    if "message" in v2w_inspector.get_table_names():
        msg_cols = {c["name"] for c in v2w_inspector.get_columns("message")}
        with engine.begin() as conn:
            for col in ("agent_key", "parent_id"):
                if col not in msg_cols:
                    conn.execute(text(f"ALTER TABLE message ADD COLUMN {col} TEXT"))

    # v2.x foundation mission: additive nullable columns on project (create_all
    # adds them on fresh DBs; ALTER for existing). Safe + idempotent.
    v2f_inspector = inspect(engine)
    if "project" in v2f_inspector.get_table_names():
        proj_cols = {c["name"] for c in v2f_inspector.get_columns("project")}
        foundation_cols = {
            "foundation_name": "TEXT",
            "foundation_spec": "TEXT",
            "foundation_set_at": "TIMESTAMP NULL",
        }
        with engine.begin() as conn:
            for col_name, col_def in foundation_cols.items():
                if col_name not in proj_cols:
                    conn.execute(text(f"ALTER TABLE project ADD COLUMN {col_name} {col_def}"))

    # v2.x Cloud Sync: cloud_sync entitlement column on tenant (dialect-aware
    # boolean default — Postgres wants FALSE, SQLite accepts 0). The new
    # SyncedConversation/SyncedMessage tables are created by create_all (which adds
    # MISSING tables on existing DBs too — same as WebAgentPresence), so no explicit
    # CREATE TABLE here. Additive + idempotent.
    vcs_inspector = inspect(engine)
    if "tenant" in vcs_inspector.get_table_names():
        tenant_cols = {c["name"] for c in vcs_inspector.get_columns("tenant")}
        if "cloud_sync" not in tenant_cols:
            default = "FALSE" if engine.dialect.name == "postgresql" else "0"
            with engine.begin() as conn:
                conn.execute(text(f"ALTER TABLE tenant ADD COLUMN cloud_sync BOOLEAN NOT NULL DEFAULT {default}"))


def get_engine() -> Engine:
    if _engine is None:
        raise RuntimeError("Engine not initialized — call init_engine(settings) first.")
    return _engine


def get_or_create_tenant(session, tenant_id: str, email: Optional[str] = None) -> "Tenant":
    """Fetch the Tenant row for tenant_id, lazily creating it on first touch.

    The legacy tenant is seeded pro_unlimited so the transitional shared-key path
    (and our own dogfood coordination) is never trial-gated. Real tenants default
    to the free plan. Commits if it created/updated a row.
    """
    from .tenant import LEGACY_TENANT
    row = session.get(Tenant, tenant_id)
    if row is None:
        row = Tenant(
            tenant_id=tenant_id,
            plan=PLAN_PRO_UNLIMITED if tenant_id == LEGACY_TENANT else PLAN_FREE,
            email=email,
        )
        session.add(row)
        session.commit()
        session.refresh(row)
    elif email and not row.email:
        row.email = email
        session.add(row)
        session.commit()
        session.refresh(row)
    return row


def cloud_sync_enabled(tenant_row: Optional["Tenant"]) -> bool:
    """Row-based Cloud Sync entitlement — the SINGLE source of truth. True when the
    tenant has the cloud_sync flag set OR is on pro_unlimited. Deliberately does NOT
    consult request identity (is_admin): entitlement is a property of the tenant ROW,
    so an admin viewing another tenant never accidentally treats it as entitled. When
    billing turns on later, a Stripe webhook just sets Tenant.cloud_sync — nothing
    downstream hardcodes plan names."""
    if tenant_row is None:
        return False
    return bool(tenant_row.cloud_sync) or tenant_row.plan == PLAN_PRO_UNLIMITED


def is_tenant_banned(tenant_id: str) -> bool:
    """True if a Tenant row exists for tenant_id and is banned. Cheap by-PK read."""
    from sqlmodel import Session
    if not tenant_id:
        return False
    with Session(get_engine()) as s:
        row = s.get(Tenant, tenant_id)
        return bool(row and row.banned)


def tenant_for_oauth_token(raw_token: str) -> Optional[str]:
    """v2.x: resolve a raw OAuth access token to its tenant_id, or None if the
    token is unknown/revoked. Used by TenantContextMiddleware to bind the tenant
    for the Claude-app MCP connector (which presents an OAuth access token, not a
    Supabase JWT). Validity/expiry is still enforced by the SDK's bearer backend;
    this is a best-effort tenant lookup only — but we still refuse revoked OR
    EXPIRED tokens here so a stale token can never bind a tenant context (defense
    in depth; matches load_access_token's revoked/expired check).
    """
    import hashlib
    import time
    from sqlmodel import Session
    h = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
    with Session(get_engine()) as s:
        row = s.get(OAuthAccessToken, h)
        if row is None or row.revoked or row.expires_at < time.time():
            return None
        return row.tenant_id
