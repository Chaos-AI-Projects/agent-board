"""Remote MCP over HTTP, mounted in board-web (MS-649, part 2c).

The board is its own OAuth authorization server: the SDK serves discovery,
dynamic registration, `/authorize`, `/token` and `/revoke` over the
provider, and `/mcp` takes a Bearer token that is either one the board
issued or an MS-631 Google access token. None of it exists without sign-in.
"""

import asyncio
import base64
import hashlib
import json
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from board import auth, core, signin, web

CHAOS = "owner@example.com"
SECRET = "s" * 32
HOST = "board.example"
ORIGIN = f"https://{HOST}"
REDIRECT = "https://claude.ai/api/mcp/auth_callback"
GOOGLE_CLIENT = "cli.apps.googleusercontent.com"
ENV = {signin.CLIENT_ID_ENV: "board.apps.googleusercontent.com",
       signin.CLIENT_SECRET_ENV: "shh", signin.SECRET_ENV: SECRET, auth.ALLOWED_ENV: CHAOS,
       auth.CLIENT_IDS_ENV: GOOGLE_CLIENT}
MCP_HEADERS = {"accept": "application/json, text/event-stream",
               "content-type": "application/json"}
VERIFIER = "v" * 64


def challenge(verifier=VERIFIER):
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")


def tokeninfo(token):
    if token == "google-token":
        return {"email": CHAOS, "email_verified": "true", "expires_in": "3600",
                "aud": GOOGLE_CLIENT}
    return None


def make_app(engine, env=ENV):
    return web.create_app(engine, authenticator=auth.Authenticator.from_env(
        env, tokeninfo=tokeninfo), sign_in=signin.SignIn.from_env(env))


@pytest.fixture
def board(migrated, monkeypatch):
    monkeypatch.delenv(web.PORT_ENV, raising=False)
    monkeypatch.setenv(web.HOSTS_ENV, HOST)
    core.create_project(migrated, "MS", "memory-solution")
    with TestClient(make_app(migrated), base_url=ORIGIN, follow_redirects=False) as c:
        yield c


def register(client):
    r = client.post("/register", json={
        "redirect_uris": [REDIRECT], "token_endpoint_auth_method": "none",
        "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"],
        "client_name": "Claude"})
    assert r.status_code == 201, r.text
    return r.json()["client_id"]


def authorize(client, client_id):
    r = client.get("/authorize", params={
        "response_type": "code", "client_id": client_id, "redirect_uri": REDIRECT,
        "code_challenge": challenge(), "code_challenge_method": "S256", "state": "st",
        "resource": f"{ORIGIN}/mcp"})
    assert r.status_code == 302, r.text
    consent = r.headers["location"]
    assert consent.startswith("/oauth/consent?")
    req = parse_qs(urlsplit(consent).query)["req"][0]
    cookie = signin.SignIn.from_env(ENV).session_cookie(auth.Identity(CHAOS, auth.HUMAN))
    client.cookies.set(signin.SESSION_COOKIE, cookie)
    r = client.post("/oauth/consent", data={"req": req, "decision": "approve"})
    client.cookies.clear()
    assert r.status_code == 303, r.text
    return parse_qs(urlsplit(r.headers["location"]).query)["code"][0]


def token(client, client_id, code, verifier=VERIFIER):
    return client.post("/token", data={
        "grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT,
        "client_id": client_id, "code_verifier": verifier})


def rpc(client, bearer, method, params=None, id=1):
    headers = dict(MCP_HEADERS)
    if bearer:
        headers["authorization"] = f"Bearer {bearer}"
    return client.post("/mcp", headers=headers, json={
        "jsonrpc": "2.0", "id": id, "method": method, "params": params or {}})


def body(r):
    """The JSON-RPC message in a JSON or single-event SSE response."""
    if r.headers.get("content-type", "").startswith("text/event-stream"):
        data = [line[5:].strip() for line in r.text.splitlines() if line.startswith("data:")]
        return json.loads(data[-1])
    return r.json()


def initialize(client, bearer):
    return rpc(client, bearer, "initialize", {
        "protocolVersion": "2025-06-18", "capabilities": {},
        "clientInfo": {"name": "test", "version": "0"}})


def create_issue(client, bearer):
    r = rpc(client, bearer, "tools/call", {"name": "create", "arguments": {
        "project": "MS", "title": "from remote"}}, id=2)
    assert r.status_code == 200, r.text
    result = body(r)["result"]
    assert not result.get("isError"), result
    return json.loads(result["content"][0]["text"])


def test_nothing_remote_without_sign_in(migrated, monkeypatch):
    monkeypatch.setenv(web.HOSTS_ENV, HOST)
    app = web.create_app(migrated, authenticator=auth.Authenticator.from_env({}), sign_in=None)
    with TestClient(app, base_url=ORIGIN) as c:
        for path in ("/.well-known/oauth-authorization-server",
                     "/.well-known/oauth-protected-resource/mcp"):
            assert c.get(path).status_code == 404
        assert rpc(c, None, "initialize").status_code in (404, 405)


def test_discovery_metadata(board):
    r = board.get("/.well-known/oauth-authorization-server")
    assert r.status_code == 200
    meta = r.json()
    assert meta["issuer"].rstrip("/") == ORIGIN
    assert meta["registration_endpoint"] == f"{ORIGIN}/register"
    assert meta["code_challenge_methods_supported"] == ["S256"]
    r = board.get("/.well-known/oauth-protected-resource/mcp")
    assert r.status_code == 200
    assert r.json()["resource"] == f"{ORIGIN}/mcp"
    assert [s.rstrip("/") for s in r.json()["authorization_servers"]] == [ORIGIN]


def test_unauthenticated_mcp_points_at_resource_metadata(board):
    r = initialize(board, None)
    assert r.status_code == 401
    assert (f'resource_metadata="{ORIGIN}/.well-known/oauth-protected-resource/mcp"'
            in r.headers["www-authenticate"])


def test_unknown_bearer_is_refused(board):
    assert initialize(board, "not-a-token").status_code == 401


def test_cross_origin_registration_is_allowed(board):
    r = board.post("/register", headers={"origin": "https://claude.ai"}, json={
        "redirect_uris": [REDIRECT], "token_endpoint_auth_method": "none"})
    assert r.status_code == 201, r.text


def test_code_flow_then_tool_call_writes_as_the_authorizing_email(board, migrated):
    client_id = register(board)
    r = token(board, client_id, authorize(board, client_id))
    assert r.status_code == 200, r.text
    access = r.json()["access_token"]
    assert initialize(board, access).status_code == 200
    made = create_issue(board, access)
    events = core.show(migrated, made["id"])["events"]
    assert (events[0]["actor"], events[0]["actor_kind"]) == (CHAOS, auth.AGENT)


def test_google_access_token_is_accepted(board, migrated):
    assert initialize(board, "google-token").status_code == 200
    made = create_issue(board, "google-token")
    assert core.show(migrated, made["id"])["events"][0]["actor"] == CHAOS


def test_token_of_an_email_since_removed_is_refused(board, migrated, monkeypatch):
    client_id = register(board)
    access = token(board, client_id, authorize(board, client_id)).json()["access_token"]
    narrowed = dict(ENV, **{auth.ALLOWED_ENV: "someone@example.com"})
    with TestClient(make_app(migrated, narrowed), base_url=ORIGIN) as c:
        assert initialize(c, access).status_code == 401


def test_wrong_pkce_verifier_is_refused(board):
    client_id = register(board)
    code = authorize(board, client_id)
    r = token(board, client_id, code, verifier="w" * 64)
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_grant"
    # A failed attempt does not spend the code for its rightful holder.
    assert token(board, client_id, code).status_code == 200


def test_redirect_uri_mismatch_is_refused(board):
    client_id = register(board)
    r = board.get("/authorize", params={
        "response_type": "code", "client_id": client_id,
        "redirect_uri": "https://evil.example/callback", "code_challenge": challenge(),
        "code_challenge_method": "S256", "state": "st"})
    assert r.status_code == 400
    assert "evil.example" not in r.headers.get("location", "")
    code = authorize(board, client_id)
    r = board.post("/token", data={
        "grant_type": "authorization_code", "code": code,
        "redirect_uri": "https://evil.example/callback", "client_id": client_id,
        "code_verifier": VERIFIER})
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_request"


def test_code_is_single_use(board):
    client_id = register(board)
    code = authorize(board, client_id)
    assert token(board, client_id, code).status_code == 200
    r = token(board, client_id, code)
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_grant"


def test_code_of_another_client_is_refused(board):
    code = authorize(board, register(board))
    r = token(board, register(board), code)
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_grant"


def refresh(client, client_id, refresh_token):
    return client.post("/token", data={
        "grant_type": "refresh_token", "refresh_token": refresh_token,
        "client_id": client_id})


def test_refresh_rotates_both_tokens(board):
    client_id = register(board)
    first = token(board, client_id, authorize(board, client_id)).json()
    r = refresh(board, client_id, first["refresh_token"])
    assert r.status_code == 200, r.text
    second = r.json()
    assert second["access_token"] != first["access_token"]
    assert second["refresh_token"] != first["refresh_token"]
    assert initialize(board, second["access_token"]).status_code == 200
    assert initialize(board, first["access_token"]).status_code == 401
    r = refresh(board, client_id, first["refresh_token"])
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_grant"


def test_revoked_token_is_refused_on_mcp(board):
    client_id = register(board)
    pair = token(board, client_id, authorize(board, client_id)).json()
    assert initialize(board, pair["access_token"]).status_code == 200
    r = board.post("/revoke", data={"token": pair["refresh_token"], "client_id": client_id})
    assert r.status_code == 200, r.text
    assert initialize(board, pair["access_token"]).status_code == 401
    assert refresh(board, client_id, pair["refresh_token"]).status_code == 400


def test_revocation_still_authenticates_a_confidential_client(board):
    r = board.post("/register", json={
        "redirect_uris": [REDIRECT], "token_endpoint_auth_method": "client_secret_post"})
    assert r.status_code == 201, r.text
    r = board.post("/revoke", data={"token": "anything", "client_id": r.json()["client_id"]})
    assert r.status_code == 401


def test_a_bearer_token_cannot_approve_consent(board):
    """Only a signed-in browser consents; a Google token alone mints no grant."""
    client_id = register(board)
    r = board.get("/authorize", params={
        "response_type": "code", "client_id": client_id, "redirect_uri": REDIRECT,
        "code_challenge": challenge(), "code_challenge_method": "S256", "state": "st"})
    req = parse_qs(urlsplit(r.headers["location"]).query)["req"][0]
    r = board.post("/oauth/consent", data={"req": req, "decision": "approve"},
                   headers={"authorization": "Bearer google-token"})
    assert r.status_code == 401
    assert "code=" not in r.headers.get("location", "")


def test_revocation_content_type_is_case_insensitive(board):
    client_id = register(board)
    pair = token(board, client_id, authorize(board, client_id)).json()
    r = board.post("/revoke", content=f"token={pair['access_token']}&client_id={client_id}",
                   headers={"content-type": "Application/X-WWW-Form-URLEncoded"})
    assert r.status_code == 200, r.text
    assert initialize(board, pair["access_token"]).status_code == 401
