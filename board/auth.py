"""Who is behind a web request, verified (MS-631).

Three credentials each resolve to one actor email:

- A signed assertion from the proxy in front of the board. Google IAP sends
  `x-goog-iap-jwt-assertion` (ES256, audience `BOARD_IAP_AUDIENCE`).
  Cloudflare Access sends `Cf-Access-Jwt-Assertion` (RS256, issuer
  `BOARD_CF_TEAM_DOMAIN`, audience `BOARD_CF_AUD`). The actor is a human.
- `Authorization: Bearer <Google OAuth access token>`, checked against
  Google's tokeninfo for its email. This path is on only when
  `BOARD_GOOGLE_CLIENT_IDS` names the OAuth clients a token may come from:
  unpinned, any site Chaos signed in to with Google could replay his token.
- `Authorization: Bearer <Google-signed ID token>` for a service account,
  audience `BOARD_SA_AUDIENCE`. A token minted without its email carries
  only the account's numeric ID in `sub`; `BOARD_SA_MAP` (`id=email,...`)
  names the email such an ID acts as (MS-656). A mapped ID is an agent.

The actor kind follows the email, whichever path proved it: a
`*.gserviceaccount.com` address is an agent and anything else is a human, so
a service account behind IAP is still an agent. The one exception is an ID
mapped by `BOARD_SA_MAP`, which is an agent whatever its email's domain. `BOARD_ALLOWED_EMAILS` gates
all three. Setting any of these variables switches the board to verified
mode, where the bare `Cf-Access-Authenticated-User-Email` header and
`BOARD_WEB_ACTOR` are ignored. A half-configured verifier refuses rather than
switching itself off, and a verifier with no allowlist refuses everyone.
With none set, the board stays in local mode and `board.web` falls back to
the header and `BOARD_WEB_ACTOR` as before.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Callable, Mapping

import jwt

ALLOWED_ENV = "BOARD_ALLOWED_EMAILS"
IAP_AUDIENCE_ENV = "BOARD_IAP_AUDIENCE"
CF_TEAM_ENV = "BOARD_CF_TEAM_DOMAIN"
CF_AUD_ENV = "BOARD_CF_AUD"
SA_AUDIENCE_ENV = "BOARD_SA_AUDIENCE"
CLIENT_IDS_ENV = "BOARD_GOOGLE_CLIENT_IDS"
SA_MAP_ENV = "BOARD_SA_MAP"

IAP_HEADER = "x-goog-iap-jwt-assertion"
CF_HEADER = "Cf-Access-Jwt-Assertion"
IAP_ISSUER = "https://cloud.google.com/iap"
IAP_KEYS_URL = "https://www.gstatic.com/iap/verify/public_key-jwk"
GOOGLE_KEYS_URL = "https://www.googleapis.com/oauth2/v3/certs"
GOOGLE_ISSUERS = ["accounts.google.com", "https://accounts.google.com"]
TOKENINFO_URL = "https://oauth2.googleapis.com/tokeninfo"
SERVICE_ACCOUNT_SUFFIX = ".gserviceaccount.com"
TIMEOUT = 5
# Tolerated clock skew between this container and the token issuer.
LEEWAY = 60
USER_AGENT = "agent-board"
CONFIG_ENVS = (ALLOWED_ENV, IAP_AUDIENCE_ENV, CF_TEAM_ENV, CF_AUD_ENV, SA_AUDIENCE_ENV,
               CLIENT_IDS_ENV, SA_MAP_ENV)

HUMAN = "human"
AGENT = "agent"

# keys(jwks_url, token) -> the public key that signed token.
Keys = Callable[[str, str], object]
# tokeninfo(access_token) -> Google's tokeninfo JSON, or None if it refused.
TokenInfo = Callable[[str], "dict | None"]


@dataclass(frozen=True)
class Identity:
    email: str
    kind: str


def cf_keys_url(team: str) -> str:
    return f"https://{team}/cdn-cgi/access/certs"


def _team(value: str) -> str:
    return value.strip().removeprefix("https://").removeprefix("http://").rstrip("/")


_jwks: dict[str, jwt.PyJWKClient] = {}


def fetch_key(url: str, token: str):
    """The signing key for token from the JWKS at url, cached per URL."""
    if url not in _jwks:
        # The JWK set is cached for 300s, so a rotated-out key stops verifying.
        _jwks[url] = jwt.PyJWKClient(url, timeout=TIMEOUT, headers={"User-Agent": USER_AGENT})
    return _jwks[url].get_signing_key_from_jwt(token).key


def fetch_tokeninfo(token: str) -> dict | None:
    """Google's view of an access token, POSTed so the token stays out of URLs."""
    body = urllib.parse.urlencode({"access_token": token}).encode()
    try:
        with urllib.request.urlopen(TOKENINFO_URL, data=body, timeout=TIMEOUT) as r:
            return json.load(r)
    except (urllib.error.URLError, ValueError, OSError):
        return None


def _split(value: str | None) -> frozenset[str]:
    return frozenset(v.strip().lower() for v in (value or "").split(",") if v.strip())


def _sa_map(value: str | None) -> dict[str, str]:
    """BOARD_SA_MAP as {service-account id: email}; a malformed entry refuses to start."""
    found: dict[str, str] = {}
    for entry in (value or "").split(","):
        if not entry.strip():
            continue
        sub, sep, email = (part.strip() for part in entry.partition("="))
        local, at, domain = email.partition("@")
        if (not sep or not sub.isdigit() or not at or not local or not domain
                or "@" in domain or any(c.isspace() for c in email)):
            raise ValueError(f"{SA_MAP_ENV}: {entry.strip()!r} is not id=email")
        if sub in found:
            raise ValueError(f"{SA_MAP_ENV}: {sub} is mapped twice")
        found[sub] = email.lower()
    return found


class Authenticator:
    def __init__(self, *, allowed=frozenset(), iap_audience=None, cf_team=None, cf_aud=None,
                 sa_audience=None, client_ids=frozenset(), sa_map=None, configured=False,
                 keys: Keys = fetch_key, tokeninfo: TokenInfo = fetch_tokeninfo):
        self.allowed = frozenset(allowed)
        self.iap_audience = iap_audience
        self.cf_team = cf_team
        self.cf_aud = cf_aud
        self.sa_audience = sa_audience
        self.client_ids = frozenset(client_ids)
        self.sa_map = dict(sa_map or {})
        self.configured = configured
        self.keys = keys
        self.tokeninfo = tokeninfo

    @classmethod
    def from_env(cls, env: Mapping[str, str], *, keys: Keys = fetch_key,
                 tokeninfo: TokenInfo = fetch_tokeninfo) -> "Authenticator":
        get = lambda name: (env.get(name) or "").strip() or None  # noqa: E731
        allowed = _split(env.get(ALLOWED_ENV))
        team, aud = get(CF_TEAM_ENV), get(CF_AUD_ENV)
        sa_map = _sa_map(env.get(SA_MAP_ENV))
        if sa_map and not get(SA_AUDIENCE_ENV):
            raise ValueError(f"{SA_MAP_ENV} is set but {SA_AUDIENCE_ENV} is not, so it maps nothing")
        return cls(allowed=allowed, iap_audience=get(IAP_AUDIENCE_ENV),
                   cf_team=_team(team) if team and aud else None,
                   cf_aud=aud if team else None,
                   sa_audience=get(SA_AUDIENCE_ENV),
                   client_ids=_split(env.get(CLIENT_IDS_ENV)),
                   sa_map=sa_map,
                   configured=any(get(name) for name in CONFIG_ENVS),
                   keys=keys, tokeninfo=tokeninfo)

    @property
    def verified(self) -> bool:
        """Verified mode: any verifier setting is present, even an incomplete one."""
        return bool(self.configured or self.allowed or self.iap_audience or self.cf_team
                    or self.sa_audience or self.client_ids or self.sa_map)

    def identify(self, headers: Mapping[str, str]) -> Identity | None:
        """The allowed actor a request's credentials prove, or None."""
        h = {k.lower(): v for k, v in headers.items()}
        found = None
        if self.iap_audience and h.get(IAP_HEADER.lower()):
            found = self._iap(h[IAP_HEADER.lower()])
        elif self.cf_team and h.get(CF_HEADER.lower()):
            found = self._cf(h[CF_HEADER.lower()])
        elif h.get("authorization", "").lower().startswith("bearer "):
            token = h["authorization"][len("bearer "):].strip()
            found = self._id_token(token) if token.count(".") == 2 else self._access(token)
        if found is None or found.email not in self.allowed:
            return None
        return found

    def _jwt(self, token, url, algorithm, **checks) -> dict | None:
        try:
            key = self.keys(url, token)
            return jwt.decode(token, key, algorithms=[algorithm],
                              leeway=LEEWAY,
                              options={"require": ["exp", "iat", "iss", "aud"]}, **checks)
        except Exception:  # any key-fetch or verification failure is a refusal
            return None

    @staticmethod
    def _email(claims: dict | None) -> Identity | None:
        email = (claims or {}).get("email")
        if not isinstance(email, str) or not email.strip():
            return None
        email = email.strip().lower()
        return Identity(email, AGENT if email.endswith(SERVICE_ACCOUNT_SUFFIX) else HUMAN)

    def _iap(self, token):
        claims = self._jwt(token, IAP_KEYS_URL, "ES256", audience=self.iap_audience,
                           issuer=IAP_ISSUER)
        return self._email(claims)

    def _cf(self, token):
        claims = self._jwt(token, cf_keys_url(self.cf_team), "RS256", audience=self.cf_aud,
                           issuer=f"https://{self.cf_team}")
        return self._email(claims)

    def _id_token(self, token):
        if not self.sa_audience:
            return None
        claims = self._jwt(token, GOOGLE_KEYS_URL, "RS256", audience=self.sa_audience,
                           issuer=GOOGLE_ISSUERS)
        if claims and str(claims.get("sub", "")) in self.sa_map:
            return Identity(self.sa_map[str(claims["sub"])], AGENT)
        if not claims or claims.get("email_verified") is not True:
            return None
        found = self._email(claims)
        # A human's ID token verifies too; this path admits only service accounts.
        return found if found and found.kind == AGENT else None

    def _access(self, token):
        if not self.client_ids or not token:
            return None
        info = self.tokeninfo(token)
        if not isinstance(info, dict) or str(info.get("email_verified")).lower() != "true":
            return None
        try:
            if int(info.get("expires_in", 0)) <= 0:
                return None
        except (TypeError, ValueError):
            return None
        if not ({str(info.get("aud", "")).lower(),
                                     str(info.get("azp", "")).lower()} & self.client_ids):
            return None
        return self._email(info)
