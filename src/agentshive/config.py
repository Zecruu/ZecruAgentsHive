import os
from dataclasses import dataclass
from typing import Optional


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
    # v2.x: Supabase project base URL (e.g. https://<ref>.supabase.co). When set,
    # the server verifies Supabase access tokens via that project's JWKS endpoint
    # and binds them to tenant_id = the token's `sub`. Optional — when unset, only
    # the legacy shared key authenticates (single-tenant "legacy" mode).
    supabase_url: Optional[str]
    legacy_key_enabled: bool = True


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
    supabase_url = (os.environ.get("SUPABASE_URL", "").strip() or None)
    legacy_key_enabled = os.environ.get("AGENTSHIVE_LEGACY_KEY_ENABLED", "1").strip().lower() not in {
        "0", "false", "no", "off",
    }
    port = int(os.environ.get("PORT", "8000"))
    poll_interval = float(os.environ.get("POLL_INTERVAL_SECONDS", "2"))
    # 240s (4 min) by default. Tools long-poll for up to this duration before returning
    # a {status: "pending"} sentinel. The bigger this is, the fewer round-trips a real
    # Coder/Planner has to make against an MCP transport whose own timeout is usually
    # higher (Claude Code allows several minutes). Keep below your MCP client's timeout.
    tool_block_timeout = float(os.environ.get("TOOL_BLOCK_TIMEOUT_SECONDS", "240"))
    return Settings(
        api_key=api_key,
        database_url=database_url,
        port=port,
        poll_interval_seconds=poll_interval,
        tool_block_timeout_seconds=tool_block_timeout,
        supabase_url=supabase_url,
        legacy_key_enabled=legacy_key_enabled,
    )
