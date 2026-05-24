import uuid
from datetime import datetime, timezone
from typing import Optional

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
    mission_id: str = Field(foreign_key="mission.id", index=True)
    direction: str = Field(index=True)
    body: str
    created_at: datetime = Field(default_factory=_utcnow)
    delivered_at: Optional[datetime] = None
    redelivery_count: Optional[int] = Field(default=0)


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
        msg_cols = {col["name"] for col in inspector.get_columns("message")}
        message_additive = {
            "redelivery_count": "INTEGER DEFAULT 0",
        }
        with engine.begin() as conn:
            for col_name, col_def in message_additive.items():
                if col_name in msg_cols:
                    continue
                conn.execute(text(f"ALTER TABLE message ADD COLUMN {col_name} {col_def}"))

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
