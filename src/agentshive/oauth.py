"""v1.7 OAuth 2.1 authorization server for AgentsHive.

Subclasses fastmcp.server.auth.OAuthProvider so the SDK can mount the full
RFC 8414 / RFC 9728 / RFC 7591 / RFC 7009 route surface for us. We only
implement the nine Protocol methods that touch storage:

    get_client, register_client,
    authorize,
    load_authorization_code, exchange_authorization_code,
    load_refresh_token,     exchange_refresh_token,
    load_access_token,
    revoke_token

Storage is the four SQLModel tables in db.py (OAuthClient, OAuthAuthorizationCode,
OAuthAccessToken, OAuthRefreshToken). Access codes, access tokens, and refresh
tokens are hashed with SHA-256 before insertion — the raw values exist only in
transit. Refresh tokens rotate on every /token exchange per OAuth 2.1 BCP for
public clients.

The consent UI itself lives in dashboard.py — `authorize()` here builds the
redirect URL into that page, and the consent page POSTs back to a
`/oauth/authorize/complete` route that finalizes by minting an authorization
code and 302'ing to the client's redirect_uri with code + state. We can't
mint the code from `authorize()` directly because the user has not yet
consented at that point.
"""

import hashlib
import hmac
import secrets
import time
from typing import Optional
from urllib.parse import urlencode

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    OAuthToken,
    RefreshToken,
)
from mcp.server.auth.settings import ClientRegistrationOptions, RevocationOptions
from mcp.shared.auth import OAuthClientInformationFull
from fastmcp.server.auth import OAuthProvider
from pydantic import AnyUrl
from sqlmodel import Session, select

from .db import (
    OAuthAccessToken as DbAccessToken,
    OAuthAuthorizationCode as DbAuthCode,
    OAuthClient as DbClient,
    OAuthRefreshToken as DbRefreshToken,
    get_engine,
)

ACCESS_TOKEN_TTL_SECONDS = 3600          # 1h — short-lived per RFC 9700 BCP
REFRESH_TOKEN_TTL_SECONDS = 30 * 86400   # 30 days
AUTH_CODE_TTL_SECONDS = 600              # 10 minutes, single-use

# Soft cap on registered DCR clients — when exceeded, the LRU (oldest
# last_used_at) row is evicted. Spec-compliant: RFC 7591 does not require
# permanence, and Claude Desktop will simply re-register if its row is gone.
MAX_REGISTERED_CLIENTS = 100


def _sha256_hex(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _new_token(n_bytes: int = 32) -> str:
    return secrets.token_urlsafe(n_bytes)


def _now_unix() -> int:
    return int(time.time())


class AgentsHiveOAuthProvider(OAuthProvider):
    """OAuth 2.1 + PKCE provider backed by SQLModel.

    Public clients only (no client_secret) for v1.7 — Claude Desktop is a
    native app, which the spec classifies as public.
    """

    def __init__(self, base_url: str, mcp_mount_path: str = "/mcp", legacy_api_key: Optional[str] = None):
        super().__init__(
            base_url=base_url,
            resource_base_url=base_url,
            issuer_url=base_url,
            client_registration_options=ClientRegistrationOptions(
                enabled=True,
                valid_scopes=["mcp"],
                default_scopes=["mcp"],
            ),
            revocation_options=RevocationOptions(enabled=True),
            required_scopes=["mcp"],
        )
        self._base_url = base_url.rstrip("/")
        # RFC 8707: the canonical resource identifier of THIS MCP server.
        # Tokens whose resource claim doesn't match this string are refused
        # at validation time, defeating confused-deputy token replay across
        # servers (Planner reminder, v1.7 scope answer).
        self._canonical_resource = f"{self._base_url}{mcp_mount_path}"
        # Q2 (KEEP LEGACY KEY FOREVER): the v1.0-v1.6 shared bearer token must
        # continue to authenticate /mcp callers. Wiring `auth=` into FastMCP
        # installs the SDK's bearer middleware which delegates to load_access_token
        # — so the cleanest place to honor the legacy key is right here. When
        # presented, we mint a synthetic AccessToken for client_id "legacy" with
        # the full mcp scope and the canonical audience.
        self._legacy_api_key = legacy_api_key

    # ----------------------------------------------------------------- clients

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        with Session(get_engine()) as s:
            row = s.get(DbClient, client_id)
            if row is None:
                return None
            return _row_to_client_info(row)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        # The SDK has already assigned client_id (and client_secret if applicable)
        # by the time it calls us — we just persist.
        with Session(get_engine()) as s:
            _evict_lru_if_over_cap(s)
            row = DbClient(
                client_id=client_info.client_id,
                client_secret=client_info.client_secret,
                client_name=client_info.client_name,
                redirect_uris=[str(u) for u in (client_info.redirect_uris or [])],
                grant_types=list(client_info.grant_types or []),
                response_types=list(client_info.response_types or []),
                scope=client_info.scope,
                token_endpoint_auth_method=client_info.token_endpoint_auth_method,
            )
            s.add(row)
            s.commit()

    # ----------------------------------------------------------------- authorize

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        """Return the URL the user-agent should follow to consent.

        We do NOT mint an authorization code here — the user has not yet said
        yes. We stash the request params in the query string of the consent
        page; the consent page POSTs back to /oauth/authorize/complete, which
        runs _mint_code() and 302s to the redirect_uri.
        """
        # RFC 8707 audience pre-check: refuse to even start a flow that would
        # issue a token for a resource that isn't us. The client is required
        # to send `resource` per the MCP spec; if it's missing we treat it as
        # an implicit request for our canonical resource (best-effort compat).
        if params.resource is not None and params.resource != self._canonical_resource:
            raise ValueError(
                f"resource indicator {params.resource!r} does not match this server's "
                f"canonical resource {self._canonical_resource!r}"
            )
        qs = urlencode({
            "client_id": client.client_id or "",
            "redirect_uri": str(params.redirect_uri),
            "redirect_uri_provided_explicitly": "1" if params.redirect_uri_provided_explicitly else "0",
            "code_challenge": params.code_challenge,
            "state": params.state or "",
            "scopes": " ".join(params.scopes or []),
            "resource": params.resource or "",
        })
        return f"{self._base_url}/oauth/consent?{qs}"

    # ----------------------------------------------------------------- auth codes

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        with Session(get_engine()) as s:
            row = s.get(DbAuthCode, _sha256_hex(authorization_code))
            if row is None or row.used or row.expires_at < _now_unix():
                return None
            if row.client_id != (client.client_id or ""):
                return None
            return AuthorizationCode(
                code=authorization_code,
                scopes=list(row.scopes or []),
                expires_at=float(row.expires_at),
                client_id=row.client_id,
                code_challenge=row.code_challenge,
                redirect_uri=AnyUrl(row.redirect_uri),
                redirect_uri_provided_explicitly=row.redirect_uri_provided_explicitly,
                resource=row.resource,
            )

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        code_hash = _sha256_hex(authorization_code.code)
        with Session(get_engine()) as s:
            row = s.get(DbAuthCode, code_hash)
            # Marking the code used must be atomic with token issuance — same
            # session, single commit. If this transaction loses to a race, the
            # second exchange will see used=True and the SDK will reject.
            if row is None or row.used:
                raise ValueError("authorization code invalid or already used")
            row.used = True
            s.add(row)

            access = _new_token()
            refresh = _new_token()
            s.add(DbAccessToken(
                token_hash=_sha256_hex(access),
                client_id=authorization_code.client_id,
                scopes=authorization_code.scopes,
                expires_at=_now_unix() + ACCESS_TOKEN_TTL_SECONDS,
                resource=authorization_code.resource,
            ))
            s.add(DbRefreshToken(
                token_hash=_sha256_hex(refresh),
                client_id=authorization_code.client_id,
                scopes=authorization_code.scopes,
                expires_at=_now_unix() + REFRESH_TOKEN_TTL_SECONDS,
            ))
            _touch_client(s, authorization_code.client_id)
            s.commit()

        return OAuthToken(
            access_token=access,
            token_type="Bearer",
            expires_in=ACCESS_TOKEN_TTL_SECONDS,
            scope=" ".join(authorization_code.scopes),
            refresh_token=refresh,
        )

    # ----------------------------------------------------------------- refresh

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        with Session(get_engine()) as s:
            row = s.get(DbRefreshToken, _sha256_hex(refresh_token))
            if row is None or row.revoked or (row.expires_at and row.expires_at < _now_unix()):
                return None
            if row.client_id != (client.client_id or ""):
                return None
            return RefreshToken(
                token=refresh_token,
                client_id=row.client_id,
                scopes=list(row.scopes or []),
                expires_at=row.expires_at,
            )

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        # OAuth 2.1 mandates refresh-token rotation for public clients: the
        # old refresh token MUST be invalidated and a new one issued. We do
        # both inside a single transaction.
        with Session(get_engine()) as s:
            old = s.get(DbRefreshToken, _sha256_hex(refresh_token.token))
            if old is None or old.revoked:
                raise ValueError("refresh token invalid")
            old.revoked = True
            s.add(old)

            granted_scopes = scopes or refresh_token.scopes

            access = _new_token()
            new_refresh = _new_token()
            s.add(DbAccessToken(
                token_hash=_sha256_hex(access),
                client_id=refresh_token.client_id,
                scopes=granted_scopes,
                expires_at=_now_unix() + ACCESS_TOKEN_TTL_SECONDS,
            ))
            s.add(DbRefreshToken(
                token_hash=_sha256_hex(new_refresh),
                client_id=refresh_token.client_id,
                scopes=granted_scopes,
                expires_at=_now_unix() + REFRESH_TOKEN_TTL_SECONDS,
            ))
            _touch_client(s, refresh_token.client_id)
            s.commit()

        return OAuthToken(
            access_token=access,
            token_type="Bearer",
            expires_in=ACCESS_TOKEN_TTL_SECONDS,
            scope=" ".join(granted_scopes),
            refresh_token=new_refresh,
        )

    # ----------------------------------------------------------------- access tokens

    async def load_access_token(self, token: str) -> AccessToken | None:
        # Legacy shared-key short-circuit — Q2: keep this path forever. Constant-time
        # compare because comparing against a secret string. Synthetic AccessToken
        # has the canonical resource so the audience check below trivially passes.
        if self._legacy_api_key and hmac.compare_digest(
            token.encode("utf-8"), self._legacy_api_key.encode("utf-8")
        ):
            return AccessToken(
                token=token,
                client_id="legacy",
                scopes=["mcp"],
                expires_at=None,
                resource=self._canonical_resource,
            )
        with Session(get_engine()) as s:
            row = s.get(DbAccessToken, _sha256_hex(token))
            if row is None or row.revoked or row.expires_at < _now_unix():
                return None
            # RFC 8707 confused-deputy defense: if the token was minted for a
            # different resource, treat it as not-our-token (do NOT just drop
            # the resource field — return None so the bearer middleware 401s).
            # A None stored resource is accepted for backward compat with
            # tokens that predate the audience claim.
            if row.resource is not None and row.resource != self._canonical_resource:
                return None
            return AccessToken(
                token=token,
                client_id=row.client_id,
                scopes=list(row.scopes or []),
                expires_at=row.expires_at,
                resource=row.resource,
            )

    # ----------------------------------------------------------------- revoke

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        token_hash = _sha256_hex(token.token)
        with Session(get_engine()) as s:
            access = s.get(DbAccessToken, token_hash)
            if access is not None:
                access.revoked = True
                s.add(access)
            refresh = s.get(DbRefreshToken, token_hash)
            if refresh is not None:
                refresh.revoked = True
                s.add(refresh)
            s.commit()

    # ----------------------------------------------------------------- consent helpers

    def mint_authorization_code(
        self,
        *,
        client_id: str,
        redirect_uri: str,
        redirect_uri_provided_explicitly: bool,
        code_challenge: str,
        scopes: list[str],
        resource: Optional[str],
    ) -> str:
        """Called by the consent-page POST handler after user approval.

        Returns the raw authorization code value (caller includes it in the
        302 to redirect_uri along with the client's state).
        """
        code = _new_token()
        with Session(get_engine()) as s:
            s.add(DbAuthCode(
                code_hash=_sha256_hex(code),
                client_id=client_id,
                scopes=scopes,
                expires_at=_now_unix() + AUTH_CODE_TTL_SECONDS,
                code_challenge=code_challenge,
                redirect_uri=redirect_uri,
                redirect_uri_provided_explicitly=redirect_uri_provided_explicitly,
                resource=resource,
            ))
            _touch_client(s, client_id)
            s.commit()
        return code


# --------------------------------------------------------------------- helpers


def _row_to_client_info(row: DbClient) -> OAuthClientInformationFull:
    return OAuthClientInformationFull(
        client_id=row.client_id,
        client_secret=row.client_secret,
        client_name=row.client_name,
        redirect_uris=[AnyUrl(u) for u in (row.redirect_uris or [])],
        grant_types=list(row.grant_types or []),
        response_types=list(row.response_types or []),
        scope=row.scope,
        token_endpoint_auth_method=row.token_endpoint_auth_method,
    )


def _touch_client(s: Session, client_id: str) -> None:
    row = s.get(DbClient, client_id)
    if row is not None:
        from datetime import datetime, timezone
        row.last_used_at = datetime.now(timezone.utc)
        s.add(row)


def _evict_lru_if_over_cap(s: Session) -> None:
    rows = list(s.exec(select(DbClient)).all())
    if len(rows) < MAX_REGISTERED_CLIENTS:
        return
    rows.sort(key=lambda r: r.last_used_at)
    for victim in rows[: len(rows) - MAX_REGISTERED_CLIENTS + 1]:
        s.delete(victim)
