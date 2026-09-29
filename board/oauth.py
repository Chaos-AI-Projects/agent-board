"""The board as an OAuth 2.1 authorization server for remote MCP (MS-649).

The MCP authorization spec, which claude.ai connectors follow, wants the
resource server to be its own authorization server, with dynamic client
registration and PKCE. The MCP SDK serves those endpoints; this module is
the storage behind them, as the SDK's `OAuthAuthorizationServerProvider`.

Who signs in is decided elsewhere, by the MS-648 Google sign-in. The SDK's
`/authorize` checks the client and redirect URI, then `authorize` sends the
browser to the board's consent page with the pending request signed into
the URL, so nothing is held in server memory between the two. Once the
browser is signed in and has consented, the web board calls `issue_code`
with the email, and the rest of the flow is the SDK's.

Every code and token is stored as its SHA-256 only, so a copy of the
database grants nothing. A code is deleted when it is exchanged, which is
what makes it single-use under a race. An access token lasts an hour and a
refresh token 30 days. A refresh rotates both. The pair one code or one
refresh produced is a grant, and revoking either token revokes the grant.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Callable
from urllib.parse import urlencode

import jwt
from pydantic import AnyUrl
from sqlalchemy import delete, select, update
from sqlalchemy.engine import Engine

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    RefreshToken,
    TokenError,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from board import store
from board.store import OAuthClient, OAuthCode, OAuthToken as TokenRow

CODE_TTL = timedelta(minutes=5)
ACCESS_TTL = timedelta(hours=1)
REFRESH_TTL = timedelta(days=30)
CONSENT_TTL = timedelta(minutes=10)
CONSENT_PATH = "/oauth/consent"
# Distinct from the sign-in cookies' audiences, so none passes as another.
CONSENT_AUDIENCE = "agent-board-oauth-consent"


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Provider:
    """Clients, codes and tokens in the board's database."""

    def __init__(self, engine: Engine, now: Callable[[], datetime] = _utcnow,
                 secret: str | None = None):
        self.engine = engine
        self.now = now
        self.secret = secret

    # --- clients ------------------------------------------------------------

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        with store.session(self.engine) as s:
            row = s.get(OAuthClient, client_id)
        if row is None:
            return None
        return OAuthClientInformationFull.model_validate_json(row.info)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        with store.session(self.engine) as s, s.begin():
            s.add(OAuthClient(client_id=client_info.client_id,
                              info=client_info.model_dump_json(), created_at=self.now()))

    # --- consent ------------------------------------------------------------

    async def authorize(self, client: OAuthClientInformationFull,
                        params: AuthorizationParams) -> str:
        """The consent page for a request the SDK has already validated."""
        if not self.secret:
            raise RuntimeError("the OAuth provider needs a secret to sign consent requests")
        now = self.now()
        req = jwt.encode({"aud": CONSENT_AUDIENCE, "iat": int(now.timestamp()),
                          "exp": int((now + CONSENT_TTL).timestamp()),
                          "client_id": client.client_id,
                          "params": params.model_dump(mode="json")},
                         self.secret, algorithm="HS256")
        return f"{CONSENT_PATH}?{urlencode({'req': req})}"

    def pending(self, req: str | None) -> tuple[str, AuthorizationParams] | None:
        """The client id and request a consent token carries, or None."""
        if not req or not self.secret:
            return None
        try:
            # Expiry is checked against self.now, not the wall clock.
            claims = jwt.decode(req, self.secret, algorithms=["HS256"],
                                audience=CONSENT_AUDIENCE,
                                options={"require": ["exp", "iat", "aud"],
                                         "verify_exp": False})
            if claims["exp"] <= self.now().timestamp():
                return None
            return str(claims["client_id"]), AuthorizationParams.model_validate(
                claims["params"])
        except (jwt.PyJWTError, KeyError, TypeError, ValueError):
            return None

    # --- codes --------------------------------------------------------------

    def issue_code(self, client_id: str, params: AuthorizationParams, email: str) -> str:
        """A code for `client_id`, to be sent back on its redirect URI."""
        code = secrets.token_urlsafe(32)
        with store.session(self.engine) as s, s.begin():
            s.add(OAuthCode(
                code_hash=_hash(code), client_id=client_id, email=email,
                scopes=" ".join(params.scopes or []), code_challenge=params.code_challenge,
                redirect_uri=str(params.redirect_uri),
                redirect_uri_explicit=params.redirect_uri_provided_explicitly,
                resource=params.resource, expires_at=self.now() + CODE_TTL,
            ))
        return code

    async def load_authorization_code(self, client: OAuthClientInformationFull,
                                      authorization_code: str) -> AuthorizationCode | None:
        with store.session(self.engine) as s:
            row = s.get(OAuthCode, _hash(authorization_code))
        if row is None or row.client_id != client.client_id or row.expires_at <= self.now():
            return None
        return AuthorizationCode(
            code=authorization_code, scopes=row.scopes.split(), client_id=row.client_id,
            expires_at=row.expires_at.timestamp(), code_challenge=row.code_challenge,
            redirect_uri=AnyUrl(row.redirect_uri),
            redirect_uri_provided_explicitly=row.redirect_uri_explicit,
            resource=row.resource, subject=row.email,
        )

    async def exchange_authorization_code(self, client: OAuthClientInformationFull,
                                          authorization_code: AuthorizationCode) -> OAuthToken:
        with store.session(self.engine) as s, s.begin():
            # The delete is the claim: of two racing exchanges one deletes
            # the row and the other deletes nothing.
            gone = s.execute(delete(OAuthCode).where(
                OAuthCode.code_hash == _hash(authorization_code.code),
                OAuthCode.client_id == client.client_id,
                OAuthCode.expires_at > self.now(),
            ))
            if gone.rowcount != 1:
                raise TokenError("invalid_grant", "authorization code is invalid or used")
            return self._grant(s, client.client_id, authorization_code.subject,
                               authorization_code.scopes, authorization_code.resource)

    # --- tokens -------------------------------------------------------------

    def _grant(self, s, client_id: str, email: str, scopes: list[str],
               resource: str | None, grant_id: str | None = None) -> OAuthToken:
        now = self.now()
        grant_id = grant_id or uuid.uuid4().hex
        access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        for secret, kind, ttl in ((access, "access", ACCESS_TTL),
                                  (refresh, "refresh", REFRESH_TTL)):
            s.add(TokenRow(token_hash=_hash(secret), kind=kind, grant_id=grant_id,
                           client_id=client_id, email=email, scopes=" ".join(scopes),
                           resource=resource, expires_at=now + ttl))
        return OAuthToken(access_token=access, token_type="Bearer",
                          expires_in=int(ACCESS_TTL.total_seconds()),
                          scope=" ".join(scopes) or None, refresh_token=refresh)

    def _live(self, s, secret: str, kind: str) -> TokenRow | None:
        row = s.get(TokenRow, _hash(secret))
        if (row is None or row.kind != kind or row.revoked_at is not None
                or row.expires_at <= self.now()):
            return None
        return row

    async def load_refresh_token(self, client: OAuthClientInformationFull,
                                 refresh_token: str) -> RefreshToken | None:
        with store.session(self.engine) as s:
            row = self._live(s, refresh_token, "refresh")
        if row is None or row.client_id != client.client_id:
            return None
        return RefreshToken(token=refresh_token, client_id=row.client_id,
                            scopes=row.scopes.split(),
                            expires_at=int(row.expires_at.timestamp()),
                            resource=row.resource, subject=row.email)

    async def exchange_refresh_token(self, client: OAuthClientInformationFull,
                                     refresh_token: RefreshToken,
                                     scopes: list[str]) -> OAuthToken:
        if not set(scopes) <= set(refresh_token.scopes):
            raise TokenError("invalid_scope", "a refresh cannot widen the grant's scopes")
        with store.session(self.engine) as s, s.begin():
            row = self._live(s, refresh_token.token, "refresh")
            if row is None or row.client_id != client.client_id:
                raise TokenError("invalid_grant", "refresh token is invalid or used")
            # Conditional on revoked_at still being NULL, so of two racing
            # refreshes only one rotates.
            claimed = s.execute(update(TokenRow).where(
                TokenRow.token_hash == row.token_hash, TokenRow.revoked_at.is_(None)
            ).values(revoked_at=self.now()))
            if claimed.rowcount != 1:
                raise TokenError("invalid_grant", "refresh token is invalid or used")
            self._revoke_grants(s, [row.grant_id])
            return self._grant(s, row.client_id, row.email, scopes or refresh_token.scopes,
                               row.resource, grant_id=row.grant_id)

    async def load_access_token(self, token: str) -> AccessToken | None:
        with store.session(self.engine) as s:
            row = self._live(s, token, "access")
        if row is None:
            return None
        return AccessToken(token=token, client_id=row.client_id, scopes=row.scopes.split(),
                           expires_at=int(row.expires_at.timestamp()),
                           resource=row.resource, subject=row.email)

    def _revoke_grants(self, s, grant_ids: list[str]) -> None:
        s.execute(update(TokenRow).where(
            TokenRow.grant_id.in_(grant_ids), TokenRow.revoked_at.is_(None)
        ).values(revoked_at=self.now()))

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        with store.session(self.engine) as s, s.begin():
            row = s.get(TokenRow, _hash(token.token))
            if row is not None:
                self._revoke_grants(s, [row.grant_id])

    def revoke_email(self, email: str) -> int:
        """Revoke every live grant `email` authorized; returns how many."""
        with store.session(self.engine) as s, s.begin():
            grants = s.scalars(select(TokenRow.grant_id).where(
                TokenRow.email == email, TokenRow.revoked_at.is_(None)).distinct()).all()
            if grants:
                self._revoke_grants(s, list(grants))
        return len(grants)
