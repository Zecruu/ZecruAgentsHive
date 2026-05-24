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


_engine: Optional[Engine] = None


def init_engine(settings: Settings) -> Engine:
    global _engine
    connect_args = {}
    if settings.database_url.startswith("sqlite"):
        connect_args = {"check_same_thread": False}
    _engine = create_engine(settings.database_url, connect_args=connect_args, pool_pre_ping=True)
    SQLModel.metadata.create_all(_engine)
    return _engine


def get_engine() -> Engine:
    if _engine is None:
        raise RuntimeError("Engine not initialized — call init_engine(settings) first.")
    return _engine
