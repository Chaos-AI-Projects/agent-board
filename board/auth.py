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
  A service account's access token minted without `userinfo.email` has no
  email, only the account's numeric ID in `azp`; when that ID is in both
  `BOARD_GOOGLE_CLIENT_IDS` and `BOARD_SA_MAP`, it acts as the mapped email
  (MS-658).
- `Authorization: Bearer <Google-signed ID token>` for a service account,
  audience `BOARD_SA_AUDIENCE`. A token minted without its email carries
  only the account's numeric ID in `sub`; `BOARD_SA_MAP` (`id=email,...`)
  names the email such an ID acts as (MS-656). A mapped ID is an agent.
  A bearer with two dots is tried here first, then against tokeninfo,
  because a service account's access token (`ya29.c.<...>`) has the same
  shape.

The actor kind follows the email, whichever path proved it: a
`*.gserviceaccount.com` address is an agent and anything else is a human, so
a service account behind IAP is still an agent. The one exception is an ID
mapped by `BOARD_SA_MAP`, which is an agent whatever its email's domain. `BOARD_ALLOWED_EMAILS` gates
all three, together with the files `BOARD_ALLOWED_EMAILS_FILES` names (MS-663,
see `Allowlist`). Setting any of these variables switches the board to verified
mode, where the bare `Cf-Access-Authenticated-User-Email` header and
`BOARD_WEB_ACTOR` are ignored. A half-configured verifier refuses rather than
switching itself off, and a verifier with no allowlist refuses everyone.
With none set, the board stays in local mode and `board.web` falls back to
the header and `BOARD_WEB_ACTOR` as before.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Callable, Mapping

import jwt

ALLOWED_ENV = "BOARD_ALLOWED_EMAILS"
ALLOWED_FILES_ENV = "BOARD_ALLOWED_EMAILS_FILES"
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
CONFIG_ENVS = (ALLOWED_ENV, ALLOWED_FILES_ENV, IAP_AUDIENCE_ENV, CF_TEAM_ENV, CF_AUD_ENV,
               SA_AUDIENCE_ENV, CLIENT_IDS_ENV, SA_MAP_ENV)

HUMAN = "human"
AGENT = "agent"

log = logging.getLogger(__name__)

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


def _stamp(path: str) -> tuple | None:
    """What changes when a file is edited, replaced or re-permissioned, or None if it is gone."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_ino, st.st_mtime_ns, st.st_ctime_ns, st.st_size)


def _read(path: str) -> frozenset[str]:
    """The emails in one allowlist file; ValueError if it cannot be read."""
    try:
        # utf-8-sig, so a byte-order mark does not hide the first email.
        with open(path, encoding="utf-8-sig") as f:
            lines = f.read().splitlines()
    except (OSError, UnicodeDecodeError) as e:
        raise ValueError(f"{ALLOWED_FILES_ENV}: cannot read {path}: {e}") from None
    return frozenset(line.strip().lower() for line in lines
                     if line.strip() and not line.strip().startswith("#"))


class Allowlist:
    """The emails allowed in: BOARD_ALLOWED_EMAILS plus the files BOARD_ALLOWED_EMAILS_FILES names.

    The files variable is a list of paths separated by `os.pathsep`. Each file
    holds one email per line; blank lines and lines starting with `#` are
    skipped, and case is ignored. A file that cannot be read at startup
    refuses to start. Every check stats the files and re-reads any that
    changed, so editing a file needs no restart. A file that cannot be read
    after startup keeps its own last good list, logs a warning and is retried
    on the next check, because an editor's atomic rename must not lock
    everyone out. The other files keep reloading meanwhile.
    """

    def __init__(self, emails=(), paths=()):
        self.emails = frozenset(e.strip().lower() for e in emails if e.strip())
        self.paths = tuple(paths)
        self._lock = threading.Lock()
        # Per file: the stamp last read successfully, its emails, and the stamp last warned about.
        self._seen = [_stamp(path) for path in self.paths]
        self._listed = [_read(path) for path in self.paths]
        # A missing file's stamp is None, so start from a value no stamp can equal.
        self._warned: list = [object()] * len(self.paths)

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "Allowlist":
        paths = [p.strip() for p in (env.get(ALLOWED_FILES_ENV) or "").split(os.pathsep)]
        return cls(_split(env.get(ALLOWED_ENV)), [p for p in paths if p])

    def current(self) -> frozenset[str]:
        if not self.paths:
            return self.emails
        with self._lock:
            for i, path in enumerate(self.paths):
                # Stamped before reading, so an edit landing mid-read is read next time.
                stamp = _stamp(path)
                if stamp == self._seen[i]:
                    continue
                try:
                    self._listed[i] = _read(path)
                except ValueError as e:
                    # Only this file keeps its last list, and it is retried on the next check.
                    if stamp != self._warned[i]:
                        log.warning("%s; keeping its last list", e)
                        self._warned[i] = stamp
                    continue
                self._seen[i], self._warned[i] = stamp, object()
            return self.emails.union(*self._listed)

    def __contains__(self, email) -> bool:
        return email in self.current()

    def __bool__(self) -> bool:
        return bool(self.current())


class Authenticator:
    def __init__(self, *, allowed=frozenset(), iap_audience=None, cf_team=None, cf_aud=None,
                 sa_audience=None, client_ids=frozenset(), sa_map=None, configured=False,
                 keys: Keys = fetch_key, tokeninfo: TokenInfo = fetch_tokeninfo):
        self.allowed = allowed if isinstance(allowed, Allowlist) else Allowlist(allowed)
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
                 tokeninfo: TokenInfo = fetch_tokeninfo,
                 allowed: Allowlist | None = None) -> "Authenticator":
        get = lambda name: (env.get(name) or "").strip() or None  # noqa: E731
        allowed = allowed if allowed is not None else Allowlist.from_env(env)
        team, aud = get(CF_TEAM_ENV), get(CF_AUD_ENV)
        sa_map = _sa_map(env.get(SA_MAP_ENV))
        # The ID-token path maps any ID; the access-token path only IDs also pinned as clients.
        client_ids = _split(env.get(CLIENT_IDS_ENV))
        if sa_map and not get(SA_AUDIENCE_ENV) and not sa_map.keys() & client_ids:
            raise ValueError(f"{SA_MAP_ENV} is set but {SA_AUDIENCE_ENV} is not and no mapped ID "
                             f"is in {CLIENT_IDS_ENV}, so it maps nothing")
        return cls(allowed=allowed, iap_audience=get(IAP_AUDIENCE_ENV),
                   cf_team=_team(team) if team and aud else None,
                   cf_aud=aud if team else None,
                   sa_audience=get(SA_AUDIENCE_ENV),
                   client_ids=client_ids,
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
            # A service account's access token can also carry two dots
            # (ya29.c.<...>), so a JWT shape is only a hint: fall back to tokeninfo.
            if token.count(".") == 2:
                found = self._id_token(token)
            found = found or self._access(token)
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
        if not isinstance(info, dict):
            return None
        try:
            if int(info.get("expires_in", 0)) <= 0:
                return None
        except (TypeError, ValueError):
            return None
        # A service account's token minted without userinfo.email names only
        # its numeric ID, in azp (MS-658). Never aud: that is the audience.
        azp = str(info.get("azp", ""))
        if azp in self.client_ids and azp in self.sa_map:
            return Identity(self.sa_map[azp], AGENT)
        if str(info.get("email_verified")).lower() != "true":
            return None
        if not ({str(info.get("aud", "")).lower(),
                                     str(info.get("azp", "")).lower()} & self.client_ids):
            return None
        return self._email(info)
