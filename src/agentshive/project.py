"""v1.9 project scoping primitives.

Multi-project namespacing pivots on a single ContextVar that the request-time
middleware sets and every `_do_*` helper / wait_for_* query reads. Putting the
contextvar in its own tiny module avoids an import cycle between `auth.py`
(which defines the middleware that sets it) and `tools.py` (which reads it
from every write path).

Reserved slugs:
  "default" — created by _apply_inline_migrations, holds all pre-v1.9 data
  and every request that arrives without a ?project= query param. Reserved
  via a server-side validator on POST /api/dashboard/projects, not a DB
  CHECK constraint (simpler to test + change later).

Slug regex (per Planner Q5 confirmation):
  ^[a-z0-9](?:[a-z0-9-]{0,40}[a-z0-9])?$
  Lowercase + digits, internal hyphens only, 1-42 chars, must start AND end
  with an alphanumeric. Matches URL-facing kebab-case conventions.
"""

import re
from contextvars import ContextVar

DEFAULT_PROJECT_SLUG = "default"

# Set by ProjectContextMiddleware on every HTTP request; read by every _do_*
# helper and every wait_for_* query lambda in tools.py. Default is "default"
# so any code path that misses the middleware (rare — direct tool invocation
# in tests, etc.) still lands in a real project that exists.
PROJECT_CONTEXT: ContextVar[str] = ContextVar("project", default=DEFAULT_PROJECT_SLUG)

# Compiled once at import; fed to re.fullmatch in the validator.
SLUG_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,40}[a-z0-9])?$")

# Today only "default" is reserved. The Planner's v1.9 nudge: defer reserving
# "api"/"oauth"/"dashboard"/"static" to v1.9.x when/if we add path-based
# routing. Adding more slugs here later only ever produces a 400 for the rare
# user trying to use one — no breaking migration.
RESERVED_SLUGS = frozenset({DEFAULT_PROJECT_SLUG})


def current_project() -> str:
    """Read the current request's project slug from the ContextVar.

    Always returns a non-empty string — falls back to DEFAULT_PROJECT_SLUG
    when the middleware hasn't run (test harnesses, sync entry points, etc.).
    """
    return PROJECT_CONTEXT.get()


def validate_slug(slug: str) -> str | None:
    """Return None if `slug` is a valid user-creatable project slug, else an error string.

    Reserved slugs ("default") return an error even though they match the regex —
    they are seeded by _apply_inline_migrations and not user-creatable.
    """
    if not isinstance(slug, str) or not slug:
        return "slug must be a non-empty string"
    if slug in RESERVED_SLUGS:
        return f"the '{slug}' slug is reserved for legacy/unscoped traffic; pick another"
    if not SLUG_PATTERN.fullmatch(slug):
        return (
            "slug must be 1-42 lowercase letters/digits with internal hyphens, "
            "no leading/trailing hyphens (e.g., 'zecru-widget', 'project-2')"
        )
    return None


# v1.11: per-Coder identity. coder_id reuses the project slug regex so a single
# source of truth governs both. None is the legacy/single-Coder case and passes
# through silently — every Coder-side tool accepts an optional coder_id and only
# validates when a value is actually supplied.
def validate_coder_id(value: str | None) -> None:
    """Validate an optional coder_id. None is legal (legacy single-Coder mode).

    Raises ValueError on a non-None value that does not match SLUG_PATTERN.
    No reserved-id list — unlike project slugs, "default" is a fine coder_id.
    """
    if value is None:
        return
    if not isinstance(value, str) or not SLUG_PATTERN.fullmatch(value):
        raise ValueError(
            "coder_id must match [a-z0-9-], 1-42 chars, no leading/trailing hyphen "
            "(e.g., 'coder-server', 'tests', 'a1')"
        )
