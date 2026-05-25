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
# - active: the one mission the Coder should be working on
# - done: Planner called mark_mission_done — Coder should stop
# - superseded: a newer mission was created, this one was replaced before completion


class Mission(SQLModel, table=True):
    id: str = Field(default_factory=_uuid, primary_key=True)
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


class Summary(SQLModel, table=True):
    id: str = Field(default_factory=_uuid, primary_key=True)
    mission_id: str = Field(foreign_key="mission.id", index=True)
    body: str
    response: Optional[str] = None
    created_at: datetime = Field(default_factory=_utcnow)
    responded_at: Optional[datetime] = None


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
    # v1.8: mission_id is OPTIONAL — the new "user_to_planner" / "planner_to_user"
    # inbox channel is global, not scoped to any mission. Mission-scoped sends
    # (coder/planner directions) still populate this field; inbox sends leave it
    # NULL if there's no active mission, OR set it to the active mission's id as
    # a soft "this happened during mission X" association.
    mission_id: Optional[str] = Field(default=None, foreign_key="mission.id", index=True)
    direction: str = Field(index=True)
    body: str
    created_at: datetime = Field(default_factory=_utcnow)
    delivered_at: Optional[datetime] = None
    redelivery_count: Optional[int] = Field(default=0)


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

    # v1.2 Feature 2: enforce "at most one active mission" at the DB level via partial unique
    # index. Belt to the supersede-and-retry suspenders in create_mission — survives app bugs.
    # Works on both SQLite (3.8+) and Postgres. Pre-cleanup first in case an old deployment
    # somehow ended up with multiple active rows (shouldn't happen with the supersede logic
    # always running, but be defensive — the CREATE INDEX would fail otherwise).
    with engine.begin() as conn:
        # Defensive cleanup: keep newest active, mark older actives 'superseded'
        rows = conn.execute(
            text("SELECT id FROM mission WHERE status='active' ORDER BY created_at DESC")
        ).fetchall()
        if len(rows) > 1:
            ids_to_demote = [r[0] for r in rows[1:]]
            for mid in ids_to_demote:
                conn.execute(
                    text("UPDATE mission SET status='superseded' WHERE id = :id"),
                    {"id": mid},
                )
        conn.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS one_active_mission "
                "ON mission (status) WHERE status = 'active'"
            )
        )


def get_engine() -> Engine:
    if _engine is None:
        raise RuntimeError("Engine not initialized — call init_engine(settings) first.")
    return _engine
