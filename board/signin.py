"""Sign in with Google, in the board itself (MS-648).

`board.auth` verifies credentials something else issued: a proxy's assertion
or a Google token. This module lets a browser sign in with no proxy at all.
`GET /login` sends it to Google with the OpenID Connect authorization-code
flow, using PKCE, `state` and `nonce`. `GET /auth/callback` exchanges the code,
verifies the ID token and sets a signed session cookie. `POST /logout` clears
that cookie.

It is off unless `BOARD_GOOGLE_OAUTH_CLIENT_ID`, `BOARD_GOOGLE_OAUTH_CLIENT_SECRET`
and `BOARD_SESSION_SECRET` are all set. Any one without the others refuses to
start, because a half-configured sign-in must not fall back to local mode.
`BOARD_ALLOWED_EMAILS` gates who may sign in, and is re-checked on every
request, so dropping an email ends that session. `BOARD_SESSION_HOURS` sets how
long a session lasts, 168 by default.

The flow's state, nonce, PKCE verifier and return path ride in a short-lived
signed cookie rather than in server memory, so a restart between the redirect
and the callback costs nothing. A callback is accepted once: its nonce is
remembered until the flow cookie would have expired anyway.

The redirect URI is derived from the request's Host, which the web board has
already checked against its allowlist: plain http on loopback, https anywhere
else, because a tunnel ends TLS before the board sees the request.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable, Mapping
from urllib.parse import urlencode, urlsplit

import jwt

from board import auth

CLIENT_ID_ENV = "BOARD_GOOGLE_OAUTH_CLIENT_ID"
CLIENT_SECRET_ENV = "BOARD_GOOGLE_OAUTH_CLIENT_SECRET"
SECRET_ENV = "BOARD_SESSION_SECRET"
HOURS_ENV = "BOARD_SESSION_HOURS"
REQUIRED_ENVS = (CLIENT_ID_ENV, CLIENT_SECRET_ENV, SECRET_ENV)
DEFAULT_HOURS = 168
MIN_SECRET_CHARS = 32

AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
CALLBACK_PATH = "/auth/callback"
SESSION_COOKIE = "board_session"
FLOW_COOKIE = "board_signin"
# Distinct audiences, so a flow cookie can never pass as a session or back.
SESSION_AUDIENCE = "agent-board-session"
FLOW_AUDIENCE = "agent-board-signin"
FLOW_SECONDS = 600
LOOPBACK = ("127.0.0.1", "localhost")

# exchange(form) -> the token endpoint's JSON, or None if it refused.
Exchange = Callable[[dict], "dict | None"]


class ConfigError(ValueError):
    """Sign-in is partly configured, or configured with an unusable value."""


def post_token(form: dict) -> dict | None:
    body = urllib.parse.urlencode(form).encode()
    req = urllib.request.Request(TOKEN_URL, data=body,
                                 headers={"User-Agent": auth.USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=auth.TIMEOUT) as r:
            return json.load(r)
    except (urllib.error.URLError, ValueError, OSError):
        return None


def safe_next(value: str | None) -> str:
    """A same-site path to return to after sign-in, else "/"."""
    if not value or not value.startswith("/") or value.startswith("//"):
        return "/"
    if "\\" in value or any(ord(c) < 32 or ord(c) == 127 for c in value):
        return "/"
    parts = urlsplit(value)
    if parts.scheme or parts.netloc:
        return "/"
    decoded = urllib.parse.unquote(value)
    if any(ord(c) < 32 or ord(c) == 127 for c in decoded):
        return "/"
    return value


def redirect_uri(host: str) -> str:
    name = host.rsplit(":", 1)[0] if not host.startswith("[") else host
    scheme = "http" if name in LOOPBACK else "https"
    return f"{scheme}://{host}{CALLBACK_PATH}"


def _same(a, b) -> bool:
    """Constant-time equality; compare_digest raises on non-ASCII str, so compare bytes."""
    return hmac.compare_digest(str(a).encode(), str(b).encode())


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


class SignIn:
    def __init__(self, *, client_id: str, client_secret: str, secret: str, hours: int,
                 allowed, keys: auth.Keys = auth.fetch_key, exchange: Exchange = post_token):
        self.client_id = client_id
        self.client_secret = client_secret
        self.secret = secret
        self.hours = hours
        self.allowed = frozenset(allowed)
        self.keys = keys
        self.exchange = exchange
        self._spent: dict[str, float] = {}
        self._lock = threading.Lock()

    @classmethod
    def from_env(cls, env: Mapping[str, str], *, keys: auth.Keys = auth.fetch_key,
                 exchange: Exchange = post_token) -> "SignIn | None":
        get = lambda name: (env.get(name) or "").strip()  # noqa: E731
        present = [name for name in REQUIRED_ENVS if get(name)]
        if not present:
            return None
        if len(present) < len(REQUIRED_ENVS):
            missing = [name for name in REQUIRED_ENVS if name not in present]
            raise ConfigError(f"Google sign-in is half configured: set {', '.join(missing)} "
                              f"too, or unset {', '.join(present)}.")
        if len(get(SECRET_ENV)) < MIN_SECRET_CHARS:
            raise ConfigError(f"{SECRET_ENV} must be at least {MIN_SECRET_CHARS} characters.")
        try:
            hours = int(get(HOURS_ENV) or DEFAULT_HOURS)
        except ValueError:
            raise ConfigError(f"{HOURS_ENV} must be a whole number of hours.") from None
        if hours <= 0:
            raise ConfigError(f"{HOURS_ENV} must be positive.")
        return cls(client_id=get(CLIENT_ID_ENV), client_secret=get(CLIENT_SECRET_ENV),
                   secret=get(SECRET_ENV), hours=hours,
                   allowed=auth._split(env.get(auth.ALLOWED_ENV)), keys=keys,
                   exchange=exchange)

    @property
    def session_seconds(self) -> int:
        return self.hours * 3600

    # --- the flow ---------------------------------------------------------------

    def start(self, host: str, next_path: str | None) -> tuple[str, str]:
        """The Google URL to send the browser to, and the flow cookie to set."""
        state, nonce, verifier = (secrets.token_urlsafe(32) for _ in range(3))
        uri = redirect_uri(host)
        now = int(time.time())
        flow = jwt.encode({"aud": FLOW_AUDIENCE, "iat": now, "exp": now + FLOW_SECONDS,
                           "state": state, "nonce": nonce, "verifier": verifier,
                           "next": safe_next(next_path), "redirect_uri": uri},
                          self.secret, algorithm="HS256")
        query = urlencode({
            "client_id": self.client_id, "response_type": "code", "scope": "openid email",
            "redirect_uri": uri, "state": state, "nonce": nonce,
            "code_challenge": _b64(hashlib.sha256(verifier.encode()).digest()),
            "code_challenge_method": "S256", "prompt": "select_account"})
        return f"{AUTHORIZE_URL}?{query}", flow

    def finish(self, params: Mapping[str, str], flow_cookie: str | None
               ) -> tuple[auth.Identity, str] | None:
        """The signed-in identity and return path for a callback, or None."""
        flow = self._decode(flow_cookie, FLOW_AUDIENCE)
        state, code = params.get("state"), params.get("code")
        if (flow is None or params.get("error") or not code or not isinstance(state, str)
                or not _same(state, flow.get("state"))):
            return None
        if not self._spend(str(flow.get("nonce")), float(flow["exp"])):
            return None
        tokens = self.exchange({"code": code, "client_id": self.client_id,
                                "client_secret": self.client_secret,
                                "redirect_uri": flow["redirect_uri"],
                                "grant_type": "authorization_code",
                                "code_verifier": flow["verifier"]})
        token = (tokens or {}).get("id_token") if isinstance(tokens, dict) else None
        if not isinstance(token, str):
            return None
        try:
            claims = jwt.decode(token, self.keys(auth.GOOGLE_KEYS_URL, token),
                                algorithms=["RS256"], audience=self.client_id,
                                issuer=auth.GOOGLE_ISSUERS, leeway=auth.LEEWAY,
                                options={"require": ["exp", "iat", "iss", "aud", "nonce"]})
        except Exception:  # any key-fetch or verification failure is a refusal
            return None
        if not _same(claims.get("nonce"), flow["nonce"]):
            return None
        if claims.get("email_verified") is not True:
            return None
        found = self._human(claims.get("email"))
        return (found, safe_next(flow.get("next"))) if found else None

    def _spend(self, nonce: str, until: float) -> bool:
        """Mark a flow used; False if it already was."""
        now = time.time()
        with self._lock:
            self._spent = {n: t for n, t in self._spent.items() if t > now}
            if nonce in self._spent:
                return False
            self._spent[nonce] = until + auth.LEEWAY
            return True

    # --- the session ------------------------------------------------------------

    def session_cookie(self, identity: auth.Identity) -> str:
        now = int(time.time())
        return jwt.encode({"aud": SESSION_AUDIENCE, "sub": identity.email, "iat": now,
                           "exp": now + self.session_seconds}, self.secret, algorithm="HS256")

    def session(self, cookie: str | None) -> auth.Identity | None:
        """The allowed human a session cookie names, or None."""
        claims = self._decode(cookie, SESSION_AUDIENCE)
        return self._human((claims or {}).get("sub"))

    def _decode(self, value: str | None, audience: str) -> dict | None:
        if not value:
            return None
        try:
            return jwt.decode(value, self.secret, algorithms=["HS256"], audience=audience,
                              options={"require": ["exp", "iat", "aud"]})
        except jwt.PyJWTError:
            return None

    def _human(self, email) -> auth.Identity | None:
        if not isinstance(email, str) or not email.strip():
            return None
        email = email.strip().lower()
        return auth.Identity(email, auth.HUMAN) if email in self.allowed else None
