import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import JSON, Column
from sqlalchemy.engine import Engine
from sqlmodel import Field, SQLModel, create_engine

from .config import Settings


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
    """
    id: str = Field(default_factory=_uuid, primary_key=True)
    # URL-facing identifier. Validated server-side (kebab-case, 1-42 chars). "default"
    # is reserved and created by _apply_inline_migrations; all other slugs come from
    # POST /api/dashboard/projects which calls project.validate_slug().
    slug: str = Field(index=True, unique=True)
    name: str  # Display name shown in the dashboard switcher; user-editable.
    description: Optional[str] = None
    created_at: datetime = Field(default_factory=_utcnow)
    # Soft-archive (Planner Q3): hides from the switcher but preserves all child data.
    # Hard delete is v1.9.x.
    archived_at: Optional[datetime] = None


class Mission(SQLModel, table=True):
    id: str = Field(default_factory=_uuid, primary_key=True)
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
    last_seen: datetime = Field(default_factory=_utcnow)
    # v1.15: device hint ("windows" | "macos" | "linux"). Surfaces on the
    # Connected Coders dashboard panel as an OS icon so the user can tell at
    # a glance which device each Coder is running on (useful when running
    # Coders across Windows + Mac in one mission). Validated server-side
    # against a strict allow-list; None means "unknown / cloud Coder".
    os_hint: Optional[str] = Field(default=None)


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


class OAuthAccessToken(SQLModel, table=True):
    token_hash: str = Field(primary_key=True)
    client_id: str = Field(index=True)
    scopes: list = Field(default_factory=list, sa_column=Column(JSON))
    expires_at: int  # unix seconds
    resource: Optional[str] = None
    revoked: bool = False
    created_at: datetime = Field(default_factory=_utcnow)


class OAuthRefreshToken(SQLModel, table=True):
    token_hash: str = Field(primary_key=True)
    client_id: str = Field(index=True)
    scopes: list = Field(default_factory=list, sa_column=Column(JSON))
    expires_at: int  # unix seconds
    revoked: bool = False
    created_at: datetime = Field(default_factory=_utcnow)


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
                    "INSERT INTO project (id, slug, name, description, created_at, archived_at) "
                    "VALUES (:id, :slug, :name, :description, :created_at, NULL)"
                ),
                {
                    "id": uuid4().hex,
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


def get_engine() -> Engine:
    if _engine is None:
        raise RuntimeError("Engine not initialized — call init_engine(settings) first.")
    return _engine
