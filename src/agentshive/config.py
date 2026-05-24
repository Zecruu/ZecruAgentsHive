import os
from dataclasses import dataclass


def _normalize_database_url(url: str) -> str:
    # Railway hands out postgres://... — SQLAlchemy 2.x wants postgresql+psycopg://...
    if url.startswith("postgres://"):
        url = "postgresql+psycopg://" + url[len("postgres://"):]
    elif url.startswith("postgresql://") and "+psycopg" not in url:
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


@dataclass(frozen=True)
class Settings:
    api_key: str
    database_url: str
    port: int
    poll_interval_seconds: float
    tool_block_timeout_seconds: float


def load_settings() -> Settings:
    api_key = os.environ.get("AGENTSHIVE_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError(
            "AGENTSHIVE_API_KEY must be set. This is the shared bearer token "
            "the Planner connector and the Coder MCP client both present."
        )
    database_url = _normalize_database_url(
        os.environ.get("DATABASE_URL", "sqlite:///./agentshive.db").strip()
    )
    port = int(os.environ.get("PORT", "8000"))
    poll_interval = float(os.environ.get("POLL_INTERVAL_SECONDS", "2"))
    tool_block_timeout = float(os.environ.get("TOOL_BLOCK_TIMEOUT_SECONDS", "50"))
    return Settings(
        api_key=api_key,
        database_url=database_url,
        port=port,
        poll_interval_seconds=poll_interval,
        tool_block_timeout_seconds=tool_block_timeout,
    )
