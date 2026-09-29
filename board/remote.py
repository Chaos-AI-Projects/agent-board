"""Remote MCP over Streamable HTTP, inside board-web (MS-649).

Everything here is the MCP SDK's, assembled over the board's pieces. The
SDK serves the authorization-server endpoints over `oauth.Provider`, and the
protected-resource metadata that tells a client where they are. `/mcp` is
the stdio server's tools behind the SDK's Bearer middleware, so a request
with no valid token gets a 401 naming that metadata.

A Bearer token is either one the board issued or, for Claude Code, an
MS-631 Google access token. The board's own tokens are re-checked against
BOARD_ALLOWED_EMAILS on every call, so removing an email cuts off the grants
it made without revoking them. The actor of a tool call is the token's
email, recorded as kind `agent`.

The issuer is `https://` plus the first BOARD_WEB_HOSTS entry, since that is
the name clients reach the board by; with none set it is the loopback URL.
"""

from __future__ import annotations

import contextlib
from typing import Callable
from urllib.parse import parse_qs

from fastapi.concurrency import run_in_threadpool
from pydantic import AnyHttpUrl
from starlette.middleware.authentication import AuthenticationMiddleware
from starlette.routing import Route

from mcp.server.auth.middleware.auth_context import AuthContextMiddleware, get_access_token
from mcp.server.auth.middleware.bearer_auth import BearerAuthBackend, RequireAuthMiddleware
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.routes import (
    build_resource_metadata_url,
    create_auth_routes,
    create_protected_resource_routes,
)
from mcp.server.auth.settings import ClientRegistrationOptions, RevocationOptions
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

from board import auth, mcp_server, oauth

MCP_PATH = "/mcp"
SCOPE = "board"
# Paths that carry their own authentication, or none because they grant
# nothing: web.py skips its sign-in redirect and same-origin check on them.
PATHS = frozenset({MCP_PATH, "/.well-known/oauth-authorization-server",
                   "/.well-known/oauth-protected-resource" + MCP_PATH,
                   "/register", "/authorize", "/token", "/revoke"})
GOOGLE_CLIENT = "google"


def issuer(hosts: list[str], loopback: str) -> str:
    return f"https://{hosts[0]}" if hosts else f"http://{loopback}"


class Verifier:
    """A Bearer token the board issued, else a Google one, for an allowed email."""

    def __init__(self, provider: oauth.Provider, authn: auth.Authenticator,
                 allowed: Callable[[str], bool]):
        self.provider = provider
        self.authn = authn
        self.allowed = allowed

    async def verify_token(self, token: str) -> AccessToken | None:
        found = await self.provider.load_access_token(token)
        if found is not None:
            return found if found.subject and self.allowed(found.subject) else None
        who = await run_in_threadpool(self.authn.identify, {"authorization": f"Bearer {token}"})
        if who is None:
            return None
        return AccessToken(token=token, client_id=GOOGLE_CLIENT, scopes=[SCOPE],
                           subject=who.email)


def public_revocation(app):
    """`app` with a missing `client_secret` sent as empty on a form POST.

    RFC 7009 has a public client send only `client_id`, which is all claude.ai
    has, but the SDK's revocation request requires the field to be present.
    An empty secret changes nothing else: a client registered with a secret
    is still refused without the right one.
    """
    async def wrapped(scope, receive, send):
        chunks, more = [], True
        while more:
            message = await receive()
            chunks.append(message.get("body", b""))
            more = message.get("more_body", False)
        body = b"".join(chunks)
        form = scope["method"] == "POST" and dict(scope["headers"]).get(
            b"content-type", b"").lower().startswith(b"application/x-www-form-urlencoded")
        if form and "client_secret" not in parse_qs(body.decode("latin-1"),
                                                    keep_blank_values=True):
            body += b"&client_secret=" if body else b"client_secret="
        sent = False

        async def replay():
            nonlocal sent
            if sent:
                return await receive()
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}

        await app(scope, replay, send)
    return wrapped


def _actor() -> str:
    token = get_access_token()
    if token is None or not token.subject:
        # RequireAuthMiddleware has already refused such a request.
        raise RuntimeError("tool call without an authenticated token")
    return token.subject


def build(engine, provider: oauth.Provider, authn: auth.Authenticator,
          allowed: Callable[[str], bool], issuer_url: str):
    """The routes to mount, and a lifespan that runs the MCP session manager."""
    base = AnyHttpUrl(issuer_url)
    resource = AnyHttpUrl(issuer_url.rstrip("/") + MCP_PATH)
    manager = StreamableHTTPSessionManager(
        app=mcp_server.build_server(engine, _actor)._mcp_server, stateless=True)

    async def handle(scope, receive, send):
        await manager.handle_request(scope, receive, send)

    guarded = AuthenticationMiddleware(
        AuthContextMiddleware(RequireAuthMiddleware(
            handle, required_scopes=[],
            resource_metadata_url=build_resource_metadata_url(resource))),
        backend=BearerAuthBackend(Verifier(provider, authn, allowed)))

    auth_routes = create_auth_routes(
        provider, base,
        client_registration_options=ClientRegistrationOptions(
            enabled=True, valid_scopes=[SCOPE], default_scopes=[SCOPE]),
        revocation_options=RevocationOptions(enabled=True))
    for route in auth_routes:
        if route.path == "/revoke":
            route.app = public_revocation(route.app)
    routes = [
        *auth_routes,
        *create_protected_resource_routes(resource, [base], scopes_supported=[SCOPE],
                                          resource_name="agent-board"),
        Route(MCP_PATH, endpoint=guarded, methods=["GET", "POST", "DELETE"]),
    ]

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        async with manager.run():
            yield

    return routes, lifespan
