"""Sign in with Google, in the board itself (MS-648).

The board runs the OIDC authorization-code flow with PKCE, `state` and
`nonce`, then keeps the signed-in email in a signed session cookie. Google's
keys are generated here and its token endpoint is stubbed, so nothing touches
the network.
"""

import base64
import hashlib
import os
import time
from http.cookies import SimpleCookie
from urllib.parse import parse_qs, urlsplit

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from board import auth, core, signin, web

CHAOS = "owner@example.com"
CLIENT_ID = "board.apps.googleusercontent.com"
SECRET = "s" * 32
ORIGIN = f"https://127.0.0.1:{web.DEFAULT_PORT}"
REDIRECT = f"http://127.0.0.1:{web.DEFAULT_PORT}/auth/callback"

GOOGLE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
ROGUE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)

ENV = {signin.CLIENT_ID_ENV: CLIENT_ID, signin.CLIENT_SECRET_ENV: "shh",
       signin.SECRET_ENV: SECRET, auth.ALLOWED_ENV: CHAOS}


def keys(url, _token):
    assert url == auth.GOOGLE_KEYS_URL
    return GOOGLE_KEY.public_key()


def id_token(nonce, email=CHAOS, aud=CLIENT_ID, verified=True, key=GOOGLE_KEY,
             iss="https://accounts.google.com"):
    now = int(time.time())
    return jwt.encode({"iss": iss, "aud": aud, "iat": now, "exp": now + 300, "nonce": nonce,
                       "email": email, "email_verified": verified},
                      key, algorithm="RS256", headers={"kid": "k1"})


class Google:
    """The token endpoint: checks the PKCE verifier, returns whatever `token` builds."""

    def __init__(self):
        self.token = lambda nonce: id_token(nonce)
        self.calls = []
        self.challenge = self.nonce = None

    def __call__(self, form):
        self.calls.append(form)
        digest = hashlib.sha256(form["code_verifier"].encode()).digest()
        if base64.urlsafe_b64encode(digest).rstrip(b"=").decode() != self.challenge:
            return None
        return {"id_token": self.token(self.nonce)}


@pytest.fixture
def board(migrated):
    core.create_project(migrated, "MS", "memory-solution")
    return migrated


@pytest.fixture
def google():
    return Google()


def make_signin(google, env=ENV, **kw):
    return signin.SignIn.from_env(env, keys=keys, exchange=google, **kw)


@pytest.fixture
def client(board, google, monkeypatch):
    monkeypatch.delenv(web.PORT_ENV, raising=False)
    monkeypatch.delenv(web.HOSTS_ENV, raising=False)
    monkeypatch.setenv(web.ACTOR_ENV, "local@example.com")
    app = web.create_app(board, authenticator=auth.Authenticator.from_env(ENV),
                         sign_in=make_signin(google))
    return TestClient(app, base_url=ORIGIN, follow_redirects=False)


def cookies(response) -> dict:
    jar = SimpleCookie()
    for header in response.headers.get_list("set-cookie"):
        jar.load(header)
    return {name: morsel for name, morsel in jar.items()}


def cookie_header(**values):
    # A browser's Accept, so a refusal is the redirect to /login rather than a 401.
    return {"cookie": "; ".join(f"{k}={v}" for k, v in values.items()),
            "accept": "text/html,application/xhtml+xml,*/*;q=0.8"}


def start(client, google, next_path="/"):
    """GET /login; returns (state, flow cookie value) and primes the stub with the nonce."""
    r = client.get("/login", params={"next": next_path})
    assert r.status_code == 303
    q = {k: v[0] for k, v in parse_qs(urlsplit(r.headers["location"]).query).items()}
    google.challenge, google.nonce = q["code_challenge"], q["nonce"]
    return q, cookies(r)[signin.FLOW_COOKIE].value


def callback(client, state, flow, code="the-code"):
    return client.get("/auth/callback", params={"code": code, "state": state},
                      headers=cookie_header(**{signin.FLOW_COOKIE: flow}))


def sign_in(client, google, next_path="/"):
    q, flow = start(client, google, next_path)
    return callback(client, q["state"], flow)


# --- the flow -----------------------------------------------------------------------


def test_login_redirects_to_google_with_pkce_state_and_nonce(client, google):
    r = client.get("/login")
    url = urlsplit(r.headers["location"])
    assert f"{url.scheme}://{url.netloc}{url.path}" == signin.AUTHORIZE_URL
    q = {k: v[0] for k, v in parse_qs(url.query).items()}
    assert q["client_id"] == CLIENT_ID
    assert q["response_type"] == "code"
    assert q["scope"] == "openid email"
    assert q["redirect_uri"] == REDIRECT
    assert q["code_challenge_method"] == "S256"
    assert len(q["state"]) >= 32 and len(q["nonce"]) >= 32 and q["code_challenge"]
    flow = cookies(r)[signin.FLOW_COOKIE]
    assert flow["httponly"] and flow["secure"] and flow["samesite"].lower() == "lax"


def test_happy_path_sets_a_session_and_returns_to_next(client, google):
    r = sign_in(client, google, "/search?q=x")
    assert r.status_code == 303
    assert r.headers["location"] == "/search?q=x"
    assert google.calls[0]["redirect_uri"] == REDIRECT
    assert google.calls[0]["client_secret"] == "shh"
    session = cookies(r)[signin.SESSION_COOKIE]
    assert session["httponly"] and session["secure"] and session["samesite"].lower() == "lax"
    assert int(session["max-age"]) == 168 * 3600
    assert cookies(r)[signin.FLOW_COOKIE].value == ""   # the flow cookie is spent

    page = client.get("/", headers=cookie_header(**{signin.SESSION_COOKIE: session.value}))
    assert page.status_code == 200
    assert CHAOS in page.text and "Sign out" in page.text


def test_a_signed_in_write_is_a_human_event(client, google, board):
    session = cookies(sign_in(client, google))[signin.SESSION_COOKIE].value
    iid = core.create(board, "MS", "item", actor=CHAOS, actor_kind="human",
                      state="ready")["id"]
    r = client.post(f"/issues/{iid}/note", data={"note": "hello"},
                    headers=cookie_header(**{signin.SESSION_COOKIE: session}))
    assert r.status_code == 303
    last = core.show(board, iid)["events"][-1]
    assert (last["actor"], last["actor_kind"]) == (CHAOS, "human")


def test_bad_state_is_refused(client, google):
    q, flow = start(client, google)
    r = callback(client, "not-the-state", flow)
    assert r.status_code == 401
    assert signin.SESSION_COOKIE not in cookies(r)
    assert google.calls == []


def test_a_callback_without_the_flow_cookie_is_refused(client, google):
    q, _flow = start(client, google)
    client.cookies.clear()   # the jar would otherwise send the flow cookie back itself
    r = client.get("/auth/callback", params={"code": "c", "state": q["state"]})
    assert r.status_code == 401


def test_a_replayed_callback_is_refused(client, google):
    q, flow = start(client, google)
    assert callback(client, q["state"], flow).status_code == 303
    again = callback(client, q["state"], flow)
    assert again.status_code == 401
    assert signin.SESSION_COOKIE not in cookies(again)


def test_a_mismatched_nonce_is_refused(client, google):
    google.token = lambda _nonce: id_token("someone-elses-nonce")
    q, flow = start(client, google)
    assert callback(client, q["state"], flow).status_code == 401


@pytest.mark.parametrize("token", [
    lambda n: id_token(n, aud="another-client.apps.googleusercontent.com"),
    lambda n: id_token(n, verified=False),
    lambda n: id_token(n, email="stranger@example.com"),
    lambda n: id_token(n, key=ROGUE_KEY),
    lambda n: id_token(n, iss="https://evil.example"),
], ids=["wrong-aud", "unverified-email", "not-allowlisted", "bad-signature", "wrong-iss"])
def test_a_bad_id_token_is_refused(client, google, token):
    google.token = token
    q, flow = start(client, google)
    r = callback(client, q["state"], flow)
    assert r.status_code == 401
    assert signin.SESSION_COOKIE not in cookies(r)


def test_google_refusing_the_code_is_refused(client, google):
    q, flow = start(client, google)
    google.challenge = "wrong"
    assert callback(client, q["state"], flow).status_code == 401


def test_a_non_ascii_state_is_refused_not_a_crash(client, google):
    _q, flow = start(client, google)
    assert callback(client, "\u00e9", flow).status_code == 401


def test_google_returning_an_error_is_refused(client, google):
    _q, flow = start(client, google)
    r = client.get("/auth/callback", params={"error": "access_denied"},
                   headers=cookie_header(**{signin.FLOW_COOKIE: flow}))
    assert r.status_code == 401


# --- the session --------------------------------------------------------------------


def test_a_tampered_session_cookie_is_refused(client, google):
    good = cookies(sign_in(client, google))[signin.SESSION_COOKIE].value
    forged = jwt.encode({"sub": "stranger@example.com", "aud": signin.SESSION_AUDIENCE,
                         "iat": int(time.time()), "exp": int(time.time()) + 600},
                        "x" * 40, algorithm="HS256")
    for value in (good[:-2] + ("AA" if good[-2:] != "AA" else "BB"), forged):
        r = client.get("/", headers=cookie_header(**{signin.SESSION_COOKIE: value}))
        assert r.status_code == 303 and r.headers["location"].startswith("/login")


def test_an_expired_session_is_refused(client, google):
    now = int(time.time())
    stale = jwt.encode({"sub": CHAOS, "aud": signin.SESSION_AUDIENCE, "iat": now - 7200,
                        "exp": now - 3600}, SECRET, algorithm="HS256")
    r = client.get("/", headers=cookie_header(**{signin.SESSION_COOKIE: stale}))
    assert r.status_code == 303 and r.headers["location"].startswith("/login")


def test_a_session_for_an_email_dropped_from_the_allowlist_is_refused(board, google):
    s = make_signin(google)
    value = s.session_cookie(auth.Identity(CHAOS, "human"))
    assert s.session(value) == auth.Identity(CHAOS, "human")
    narrowed = make_signin(google, env=ENV | {auth.ALLOWED_ENV: "someone@example.com"})
    assert narrowed.session(value) is None


def test_the_flow_cookie_is_not_a_session(client, google):
    _q, flow = start(client, google)
    r = client.get("/", headers=cookie_header(**{signin.SESSION_COOKIE: flow}))
    assert r.status_code == 303


def test_session_hours_sets_the_lifetime(google):
    s = make_signin(google, env=ENV | {signin.HOURS_ENV: "2"})
    claims = jwt.decode(s.session_cookie(auth.Identity(CHAOS, "human")), SECRET,
                        algorithms=["HS256"], audience=signin.SESSION_AUDIENCE)
    assert claims["exp"] - claims["iat"] == 2 * 3600


def test_logout_clears_the_session(client, google):
    session = cookies(sign_in(client, google))[signin.SESSION_COOKIE].value
    r = client.post("/logout", headers=cookie_header(**{signin.SESSION_COOKIE: session}))
    assert r.status_code == 200
    gone = cookies(r)[signin.SESSION_COOKIE]
    assert gone.value == "" and int(gone["max-age"]) == 0
    assert "Sign in" in r.text


def test_a_cross_origin_logout_is_refused(client, google):
    r = client.post("/logout", headers={"origin": "https://evil.example"})
    assert r.status_code == 403


# --- who gets in ----------------------------------------------------------------------


def test_an_anonymous_browser_get_goes_to_login_with_its_path(client):
    r = client.get("/issues/MS-1?x=1", headers={"accept": "text/html"})
    assert r.status_code == 303
    assert r.headers["location"] == "/login?next=%2Fissues%2FMS-1%3Fx%3D1"


def test_an_anonymous_api_get_or_post_gets_401(client):
    assert client.get("/", headers={"accept": "application/json"}).status_code == 401
    assert client.post("/issues", data={"title": "x"}).status_code == 401


def test_sign_in_switches_off_local_mode(board, google, monkeypatch):
    # No BOARD_ALLOWED_EMAILS, so board.auth alone would stay in local mode.
    # BOARD_WEB_ACTOR and the bare Access header are set, and neither counts.
    monkeypatch.delenv(web.PORT_ENV, raising=False)
    monkeypatch.delenv(web.HOSTS_ENV, raising=False)
    monkeypatch.setenv(web.ACTOR_ENV, "local@example.com")
    env = {k: v for k, v in ENV.items() if k != auth.ALLOWED_ENV}
    authn = auth.Authenticator.from_env(env)
    assert not authn.verified
    app = web.create_app(board, authenticator=authn, sign_in=make_signin(google, env=env))
    c = TestClient(app, base_url=ORIGIN, follow_redirects=False)
    r = c.post("/issues", data={"title": "x"}, headers={web.ACTOR_HEADER: CHAOS})
    assert r.status_code == 401


def test_a_bearer_token_still_works_beside_sign_in(board, google, monkeypatch):
    monkeypatch.delenv(web.PORT_ENV, raising=False)
    monkeypatch.delenv(web.HOSTS_ENV, raising=False)
    env = ENV | {auth.CLIENT_IDS_ENV: "cli.apps"}
    info = {"email": CHAOS, "email_verified": "true", "expires_in": "3599",
            "azp": "cli.apps", "aud": "cli.apps"}
    authn = auth.Authenticator.from_env(env, tokeninfo=lambda t: info if t == "tok" else None)
    app = web.create_app(board, authenticator=authn, sign_in=make_signin(google, env=env))
    c = TestClient(app, base_url=ORIGIN, follow_redirects=False)
    assert c.get("/", headers={"authorization": "Bearer tok"}).status_code == 200


@pytest.mark.parametrize("bad", [
    "https://evil.example/", "//evil.example/x", "/\\evil.example", "javascript:alert(1)",
    "evil.example", "", "/%0d%0aSet-Cookie:x=1"])
def test_an_open_redirect_next_is_refused(client, google, bad):
    r = sign_in(client, google, bad)
    assert r.status_code == 303
    assert r.headers["location"] == "/"


def test_safe_next_keeps_a_same_site_path():
    assert signin.safe_next("/issues/MS-1?x=1#y") == "/issues/MS-1?x=1#y"


# --- configuration --------------------------------------------------------------------


def test_sign_in_is_off_with_no_settings(google):
    assert signin.SignIn.from_env({}, keys=keys, exchange=google) is None


@pytest.mark.parametrize("missing", [signin.CLIENT_ID_ENV, signin.CLIENT_SECRET_ENV,
                                     signin.SECRET_ENV])
def test_a_partial_config_refuses_to_start(board, monkeypatch, missing):
    env = {k: v for k, v in ENV.items() if k != missing}
    with pytest.raises(signin.ConfigError):
        signin.SignIn.from_env(env)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv(missing, raising=False)
    with pytest.raises(signin.ConfigError):
        web.create_app(board)


def test_a_short_session_secret_refuses_to_start(google):
    with pytest.raises(signin.ConfigError):
        make_signin(google, env=ENV | {signin.SECRET_ENV: "short"})


def test_a_bad_session_hours_refuses_to_start(google):
    for bad in ("0", "-1", "soon"):
        with pytest.raises(signin.ConfigError):
            make_signin(google, env=ENV | {signin.HOURS_ENV: bad})


def test_a_session_for_an_email_dropped_from_a_file_is_refused(board, google, tmp_path):
    listed = tmp_path / "allowed"
    listed.write_text(f"{CHAOS}\n")
    env = {k: v for k, v in ENV.items() if k != auth.ALLOWED_ENV}
    s = make_signin(google, env=env | {auth.ALLOWED_FILES_ENV: str(listed)})
    value = s.session_cookie(auth.Identity(CHAOS, "human"))
    assert s.session(value) == auth.Identity(CHAOS, "human")
    listed.write_text("someone-else@example.com\n")
    st = listed.stat()
    os.utime(listed, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
    assert s.session(value) is None
