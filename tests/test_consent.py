"""The consent step of the board's OAuth flow for remote MCP (MS-649).

The SDK's `/authorize` validates the client and redirect URI, then asks the
provider where to send the browser. The provider answers with the board's
consent page, carrying the pending request as a signed token. A signed-in
human approves, which issues a code to the client's redirect URI, or denies,
which sends back `error=access_denied`.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient
from pydantic import AnyUrl

from mcp.server.auth.provider import AuthorizationParams
from mcp.shared.auth import OAuthClientInformationFull

from board import auth, core, oauth, signin, web

CHAOS = "owner@example.com"
SECRET = "s" * 32
ORIGIN = f"https://127.0.0.1:{web.DEFAULT_PORT}"
REDIRECT = "https://claude.ai/api/mcp/auth_callback"
ENV = {signin.CLIENT_ID_ENV: "board.apps.googleusercontent.com",
       signin.CLIENT_SECRET_ENV: "shh", signin.SECRET_ENV: SECRET, auth.ALLOWED_ENV: CHAOS}
HTML = "text/html,application/xhtml+xml,*/*;q=0.8"


def run(coro):
    return asyncio.run(coro)


class Clock:
    def __init__(self):
        self.t = datetime(2026, 9, 29, 3, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.t


def client_info():
    return OAuthClientInformationFull(
        client_id="c1", client_id_issued_at=1, redirect_uris=[AnyUrl(REDIRECT)],
        token_endpoint_auth_method="none", grant_types=["authorization_code", "refresh_token"],
        response_types=["code"], client_name="Claude <connector>")


def params(state="st"):
    return AuthorizationParams(
        state=state, scopes=["board"], code_challenge="x" * 43, redirect_uri=AnyUrl(REDIRECT),
        redirect_uri_provided_explicitly=True, resource="https://board.example/mcp")


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def app(migrated, clock, monkeypatch):
    monkeypatch.delenv(web.PORT_ENV, raising=False)
    monkeypatch.delenv(web.HOSTS_ENV, raising=False)
    core.create_project(migrated, "MS", "memory-solution")
    app = web.create_app(migrated, authenticator=auth.Authenticator.from_env(ENV),
                         sign_in=signin.SignIn.from_env(ENV))
    app.state.oauth.now = clock
    run(app.state.oauth.register_client(client_info()))
    return app


@pytest.fixture
def client(app):
    return TestClient(app, base_url=ORIGIN, follow_redirects=False)


def signed_in(app):
    cookie = signin.SignIn.from_env(ENV).session_cookie(auth.Identity(CHAOS, auth.HUMAN))
    return {"cookie": f"{signin.SESSION_COOKIE}={cookie}", "accept": HTML}


def consent_url(app, state="st"):
    return run(app.state.oauth.authorize(client_info(), params(state)))


def req_of(url):
    return parse_qs(urlsplit(url).query)["req"][0]


def query(location):
    return {k: v[0] for k, v in parse_qs(urlsplit(location).query).items()}


def test_authorize_sends_the_browser_to_the_consent_page(app):
    url = consent_url(app)
    assert urlsplit(url).path == "/oauth/consent"
    assert req_of(url)


def test_a_signed_out_browser_is_sent_to_login_and_back(app, client):
    url = consent_url(app)
    r = client.get(url, headers={"accept": HTML})
    assert r.status_code == 303
    location = urlsplit(r.headers["location"])
    assert location.path == "/login"
    assert query(r.headers["location"])["next"] == url


def test_the_consent_page_names_the_client_and_redirect_uri(app, client):
    r = client.get(consent_url(app), headers=signed_in(app))
    assert r.status_code == 200
    assert "Claude &lt;connector&gt;" in r.text
    assert REDIRECT in r.text
    assert CHAOS in r.text


def test_approving_issues_a_code_for_the_signed_in_email(app, client):
    req = req_of(consent_url(app))
    r = client.post("/oauth/consent", data={"req": req, "decision": "approve"},
                    headers=signed_in(app))
    assert r.status_code == 303
    location = r.headers["location"]
    assert location.startswith(REDIRECT + "?")
    q = query(location)
    assert q["state"] == "st"
    code = run(app.state.oauth.load_authorization_code(client_info(), q["code"]))
    assert code.subject == CHAOS
    assert code.code_challenge == "x" * 43
    assert code.scopes == ["board"]
    assert code.resource == "https://board.example/mcp"


def test_denying_returns_access_denied_and_issues_nothing(app, client):
    req = req_of(consent_url(app))
    r = client.post("/oauth/consent", data={"req": req, "decision": "deny"},
                    headers=signed_in(app))
    assert r.status_code == 303
    assert query(r.headers["location"]) == {"error": "access_denied", "state": "st"}


def test_a_request_with_no_state_comes_back_without_one(app, client):
    req = req_of(consent_url(app, state=None))
    r = client.post("/oauth/consent", data={"req": req, "decision": "deny"},
                    headers=signed_in(app))
    assert query(r.headers["location"]) == {"error": "access_denied"}


@pytest.mark.parametrize("method", ["get", "post"])
def test_a_tampered_request_is_refused(app, client, method):
    req = req_of(consent_url(app))
    bad = req[:-2] + ("AA" if not req.endswith("AA") else "BB")
    if method == "get":
        r = client.get("/oauth/consent", params={"req": bad}, headers=signed_in(app))
    else:
        r = client.post("/oauth/consent", data={"req": bad, "decision": "approve"},
                        headers=signed_in(app))
    assert r.status_code == 400


def test_a_request_older_than_ten_minutes_is_refused(app, client, clock):
    req = req_of(consent_url(app))
    clock.t += timedelta(minutes=10, seconds=1)
    r = client.post("/oauth/consent", data={"req": req, "decision": "approve"},
                    headers=signed_in(app))
    assert r.status_code == 400


def test_a_session_cookie_is_not_a_consent_request(app, client):
    cookie = signin.SignIn.from_env(ENV).session_cookie(auth.Identity(CHAOS, auth.HUMAN))
    r = client.get("/oauth/consent", params={"req": cookie}, headers=signed_in(app))
    assert r.status_code == 400


def test_a_signed_out_post_issues_no_code(app, client):
    req = req_of(consent_url(app))
    r = client.post("/oauth/consent", data={"req": req, "decision": "approve"})
    assert r.status_code == 401


def test_a_cross_origin_approval_is_refused(app, client):
    req = req_of(consent_url(app))
    r = client.post("/oauth/consent", data={"req": req, "decision": "approve"},
                    headers={**signed_in(app), "origin": "https://evil.example"})
    assert r.status_code == 403


def test_an_unknown_decision_issues_no_code(app, client):
    req = req_of(consent_url(app))
    r = client.post("/oauth/consent", data={"req": req, "decision": "maybe"},
                    headers=signed_in(app))
    assert r.status_code == 400


def test_there_is_no_consent_page_without_sign_in(migrated, monkeypatch):
    monkeypatch.delenv(web.PORT_ENV, raising=False)
    monkeypatch.delenv(web.HOSTS_ENV, raising=False)
    monkeypatch.setenv(web.ACTOR_ENV, "local@example.com")
    app = web.create_app(migrated, authenticator=auth.Authenticator.from_env({}), sign_in=None)
    assert getattr(app.state, "oauth", None) is None
    r = TestClient(app, base_url=ORIGIN).get("/oauth/consent", params={"req": "x"})
    assert r.status_code == 404


def test_a_provider_without_a_secret_cannot_authorize(migrated):
    with pytest.raises(RuntimeError):
        run(oauth.Provider(migrated).authorize(client_info(), params()))


def test_the_consent_page_cannot_be_framed(app, client):
    r = client.get(consent_url(app), headers=signed_in(app))
    assert r.status_code == 200
    assert "frame-ancestors 'none'" in r.headers.get("content-security-policy", "")
