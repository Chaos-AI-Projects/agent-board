"""The board as an OAuth 2.1 authorization server for remote MCP (MS-649).

These cover the storage half: clients, authorization codes and tokens, as
the MCP SDK's provider interface sees them. Every secret is stored as its
SHA-256 only, so the tests also look in the database for the raw value.
"""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import AnyUrl
from sqlalchemy import text

from mcp.server.auth.provider import AuthorizationParams, TokenError
from mcp.shared.auth import OAuthClientInformationFull

from board import oauth

EMAIL = "owner@example.com"
REDIRECT = "https://claude.ai/api/mcp/auth_callback"


def run(coro):
    return asyncio.run(coro)


class Clock:
    def __init__(self):
        self.t = datetime(2026, 9, 29, 3, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.t

    def advance(self, **kw):
        self.t += timedelta(**kw)


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def provider(migrated, clock):
    return oauth.Provider(migrated, now=clock)


def client_info(client_id="c1"):
    return OAuthClientInformationFull(
        client_id=client_id,
        client_id_issued_at=1,
        redirect_uris=[AnyUrl(REDIRECT)],
        token_endpoint_auth_method="none",
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        client_name="Claude",
    )


def params(scopes=("board",)):
    return AuthorizationParams(
        state="st", scopes=list(scopes), code_challenge="x" * 43,
        redirect_uri=AnyUrl(REDIRECT), redirect_uri_provided_explicitly=True,
        resource="https://board.example/mcp",
    )


def dump(engine):
    out = []
    with engine.connect() as conn:
        for table in ("oauth_client", "oauth_code", "oauth_token"):
            for row in conn.execute(text(f"SELECT * FROM {table}")):
                out.append(" ".join(str(v) for v in row))
    return "\n".join(out)


@pytest.fixture
def registered(provider):
    client = client_info()
    run(provider.register_client(client))
    return client


@pytest.fixture
def granted(provider, registered):
    code = provider.issue_code(registered.client_id, params(), EMAIL)
    loaded = run(provider.load_authorization_code(registered, code))
    return run(provider.exchange_authorization_code(registered, loaded))


def test_registered_client_round_trips(provider, registered):
    got = run(provider.get_client("c1"))
    assert got.client_id == "c1"
    assert [str(u) for u in got.redirect_uris] == [REDIRECT]
    assert got.client_name == "Claude"
    assert run(provider.get_client("nope")) is None


def test_code_loads_with_the_signed_in_email_and_is_stored_hashed(provider, registered, migrated):
    code = provider.issue_code("c1", params(), EMAIL)
    assert len(code) >= 32
    loaded = run(provider.load_authorization_code(registered, code))
    assert loaded.subject == EMAIL
    assert loaded.client_id == "c1"
    assert loaded.code_challenge == "x" * 43
    assert str(loaded.redirect_uri) == REDIRECT
    assert loaded.scopes == ["board"]
    assert loaded.resource == "https://board.example/mcp"
    assert code not in dump(migrated)


def test_code_is_bound_to_its_client(provider, registered):
    run(provider.register_client(client_info("c2")))
    code = provider.issue_code("c1", params(), EMAIL)
    assert run(provider.load_authorization_code(client_info("c2"), code)) is None


def test_code_expires(provider, registered, clock):
    code = provider.issue_code("c1", params(), EMAIL)
    clock.advance(seconds=oauth.CODE_TTL.total_seconds() + 1)
    assert run(provider.load_authorization_code(registered, code)) is None


def test_code_is_single_use(provider, registered):
    code = provider.issue_code("c1", params(), EMAIL)
    loaded = run(provider.load_authorization_code(registered, code))
    run(provider.exchange_authorization_code(registered, loaded))
    assert run(provider.load_authorization_code(registered, code)) is None
    with pytest.raises(TokenError):
        run(provider.exchange_authorization_code(registered, loaded))


def test_exchange_issues_an_hour_access_token_and_a_refresh_token(provider, granted, migrated):
    assert granted.token_type == "Bearer"
    assert granted.expires_in == 3600
    assert granted.refresh_token
    assert granted.scope == "board"
    access = run(provider.load_access_token(granted.access_token))
    assert access.subject == EMAIL
    assert access.client_id == "c1"
    assert access.resource == "https://board.example/mcp"
    dumped = dump(migrated)
    assert granted.access_token not in dumped
    assert granted.refresh_token not in dumped


def test_access_token_expires_after_an_hour(provider, granted, clock):
    clock.advance(minutes=61)
    assert run(provider.load_access_token(granted.access_token)) is None


def test_refresh_token_lasts_thirty_days(provider, registered, granted, clock):
    clock.advance(days=29)
    assert run(provider.load_refresh_token(registered, granted.refresh_token)) is not None
    clock.advance(days=2)
    assert run(provider.load_refresh_token(registered, granted.refresh_token)) is None


def test_refresh_token_is_bound_to_its_client(provider, granted):
    other = client_info("c2")
    run(provider.register_client(other))
    assert run(provider.load_refresh_token(other, granted.refresh_token)) is None


def test_access_token_is_not_a_refresh_token(provider, registered, granted):
    assert run(provider.load_refresh_token(registered, granted.access_token)) is None
    assert run(provider.load_access_token(granted.refresh_token)) is None


def test_refresh_rotates_both_tokens(provider, registered, granted):
    old = run(provider.load_refresh_token(registered, granted.refresh_token))
    new = run(provider.exchange_refresh_token(registered, old, ["board"]))
    assert new.access_token != granted.access_token
    assert new.refresh_token != granted.refresh_token
    assert run(provider.load_refresh_token(registered, granted.refresh_token)) is None
    assert run(provider.load_access_token(granted.access_token)) is None
    assert run(provider.load_access_token(new.access_token)).subject == EMAIL
    with pytest.raises(TokenError):
        run(provider.exchange_refresh_token(registered, old, ["board"]))


def test_refresh_cannot_widen_scopes(provider, registered, granted):
    old = run(provider.load_refresh_token(registered, granted.refresh_token))
    with pytest.raises(TokenError) as err:
        run(provider.exchange_refresh_token(registered, old, ["board", "admin"]))
    assert err.value.error == "invalid_scope"


@pytest.mark.parametrize("which", ["access", "refresh"])
def test_revoking_either_token_revokes_the_grant(provider, registered, granted, which):
    access = run(provider.load_access_token(granted.access_token))
    refresh = run(provider.load_refresh_token(registered, granted.refresh_token))
    run(provider.revoke_token(access if which == "access" else refresh))
    assert run(provider.load_access_token(granted.access_token)) is None
    assert run(provider.load_refresh_token(registered, granted.refresh_token)) is None
    run(provider.revoke_token(access))  # a second revoke is a no-op


def test_revoke_all_for_email(provider, granted):
    assert provider.revoke_email(EMAIL) == 1
    assert run(provider.load_access_token(granted.access_token)) is None
