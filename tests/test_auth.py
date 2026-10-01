"""Verified identity for the web board (MS-631).

Three credentials each resolve to one actor email: a signed IAP or Cloudflare
Access assertion (human), a Google OAuth access token checked against
tokeninfo (human, or a mapped service account's agent), and a Google
service-account ID token (agent). Keys are
generated here and tokeninfo is stubbed, so nothing touches the network.
"""

import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from fastapi.testclient import TestClient

from board import auth, core, web

CHAOS = "owner@example.com"
BOT = "board-agent@proj.iam.gserviceaccount.com"
IAP_AUD = "/projects/1/global/backendServices/2"
CF_TEAM = "chaos.cloudflareaccess.com"
CF_AUD = "cf-aud-tag"
SA_AUD = "https://board.example.com"
LOCAL = f"http://127.0.0.1:{web.DEFAULT_PORT}"

IAP_KEY = ec.generate_private_key(ec.SECP256R1())
CF_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
GOOGLE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
ROGUE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)

KEYS = {auth.IAP_KEYS_URL: IAP_KEY.public_key(),
        auth.cf_keys_url(CF_TEAM): CF_KEY.public_key(),
        auth.GOOGLE_KEYS_URL: GOOGLE_KEY.public_key()}


def keys(url, _token):
    return KEYS[url]


def sign(key, alg, **claims):
    now = int(time.time())
    return jwt.encode({"iat": now, "exp": now + 300} | claims, key, algorithm=alg,
                      headers={"kid": "k1"})


def iap(email=CHAOS, aud=IAP_AUD, key=IAP_KEY, iss=auth.IAP_ISSUER, **kw):
    return sign(key, "ES256", iss=iss, aud=aud, email=email, **kw)


def cf(email=CHAOS, aud=CF_AUD, key=CF_KEY, iss=f"https://{CF_TEAM}", **kw):
    return sign(key, "RS256", iss=iss, aud=aud, email=email, **kw)


def sa(email=BOT, aud=SA_AUD, key=GOOGLE_KEY, verified=True,
       iss="https://accounts.google.com", **kw):
    return sign(key, "RS256", iss=iss, aud=aud, email=email, email_verified=verified, **kw)


CLIENT = "cli.apps.googleusercontent.com"


def info(email=CHAOS, verified="true", expires_in="3599", client=CLIENT):
    return {"email": email, "email_verified": verified, "expires_in": expires_in,
            "azp": client, "aud": client}


TOKENINFO = {"good-token": info(), "stranger-token": info(email="x@evil.test"),
             "unverified-token": info(verified="false"),
             "expired-token": info(expires_in="0"),
             "other-client-token": info(client="some-other-site.apps"),
             "no-email-token": info() | {"email": None}}


def tokeninfo(token):
    return TOKENINFO.get(token)


def authn(env):
    return auth.Authenticator.from_env(env, keys=keys, tokeninfo=tokeninfo)


FULL = {auth.ALLOWED_ENV: f"{CHAOS}, {BOT}", auth.IAP_AUDIENCE_ENV: IAP_AUD,
        auth.CF_TEAM_ENV: CF_TEAM, auth.CF_AUD_ENV: CF_AUD, auth.SA_AUDIENCE_ENV: SA_AUD,
        auth.CLIENT_IDS_ENV: CLIENT}


def who(env, headers):
    return authn(env).identify(headers)


# --- the three credentials ------------------------------------------------------


def test_a_signed_iap_assertion_is_a_human():
    assert who(FULL, {auth.IAP_HEADER: iap()}) == auth.Identity(CHAOS, "human")


def test_an_iap_assertion_for_another_audience_is_refused():
    assert who(FULL, {auth.IAP_HEADER: iap(aud="/projects/9/other")}) is None


def test_an_iap_assertion_signed_by_another_key_is_refused():
    rogue = ec.generate_private_key(ec.SECP256R1())
    assert who(FULL, {auth.IAP_HEADER: iap(key=rogue)}) is None


def test_an_expired_iap_assertion_is_refused():
    old = int(time.time()) - 3600
    assert who(FULL, {auth.IAP_HEADER: iap(iat=old, exp=old + 60)}) is None


def test_a_signed_cloudflare_assertion_is_a_human():
    assert who(FULL, {auth.CF_HEADER: cf()}) == auth.Identity(CHAOS, "human")


def test_a_cloudflare_assertion_for_another_aud_is_refused():
    assert who(FULL, {auth.CF_HEADER: cf(aud="someone-else")}) is None


def test_the_bare_cloudflare_email_header_is_ignored_once_a_verifier_is_set():
    assert who(FULL, {web.ACTOR_HEADER: CHAOS}) is None


def test_a_google_access_token_is_a_human():
    assert who(FULL, {"Authorization": "Bearer good-token"}) == auth.Identity(CHAOS, "human")


def test_an_access_token_tokeninfo_rejects_is_refused():
    assert who(FULL, {"Authorization": "Bearer made-up"}) is None


def test_an_access_token_with_an_unverified_email_is_refused():
    assert who(FULL, {"Authorization": "Bearer unverified-token"}) is None


def test_a_service_account_id_token_is_an_agent():
    assert who(FULL, {"Authorization": f"Bearer {sa()}"}) == auth.Identity(BOT, "agent")


def test_a_service_account_token_for_another_audience_is_refused():
    assert who(FULL, {"Authorization": f"Bearer {sa(aud='https://other')}"}) is None


def test_a_service_account_token_signed_by_another_key_is_refused():
    assert who(FULL, {"Authorization": f"Bearer {sa(key=ROGUE_KEY)}"}) is None


def test_a_service_account_token_with_an_unverified_email_is_refused():
    assert who(FULL, {"Authorization": f"Bearer {sa(verified=False)}"}) is None


def test_a_human_id_token_is_not_an_agent():
    env = FULL | {auth.ALLOWED_ENV: CHAOS}
    assert who(env, {"Authorization": f"Bearer {sa(email=CHAOS)}"}) is None


def test_an_access_token_from_an_unpinned_client_is_refused():
    assert who(FULL, {"Authorization": "Bearer other-client-token"}) is None


def test_access_tokens_are_off_until_a_client_is_pinned():
    env = {k: v for k, v in FULL.items() if k != auth.CLIENT_IDS_ENV}
    assert who(env, {"Authorization": "Bearer good-token"}) is None


def test_an_expired_or_emailless_access_token_is_refused():
    assert who(FULL, {"Authorization": "Bearer expired-token"}) is None
    assert who(FULL, {"Authorization": "Bearer no-email-token"}) is None


def test_a_service_account_behind_iap_is_still_an_agent():
    assert who(FULL, {auth.IAP_HEADER: iap(email=BOT)}) == auth.Identity(BOT, "agent")


def test_a_wrong_issuer_is_refused_on_every_jwt_path():
    assert who(FULL, {auth.IAP_HEADER: iap(iss="https://evil.test")}) is None
    assert who(FULL, {auth.CF_HEADER: cf(iss="https://evil.cloudflareaccess.com")}) is None
    assert who(FULL, {"Authorization": f"Bearer {sa(iss='https://evil.test')}"}) is None


def test_expired_cloudflare_and_service_account_tokens_are_refused():
    old = int(time.time()) - 3600
    assert who(FULL, {auth.CF_HEADER: cf(iat=old, exp=old + 60)}) is None
    assert who(FULL, {"Authorization": f"Bearer {sa(iat=old, exp=old + 60)}"}) is None


def test_an_unsigned_assertion_is_refused():
    token = jwt.encode({"iss": auth.IAP_ISSUER, "aud": IAP_AUD, "email": CHAOS,
                        "iat": int(time.time()), "exp": int(time.time()) + 300},
                       None, algorithm="none")
    assert who(FULL, {auth.IAP_HEADER: token}) is None


def test_an_id_token_is_refused_when_no_service_account_audience_is_set():
    env = {k: v for k, v in FULL.items() if k != auth.SA_AUDIENCE_ENV}
    assert who(env, {"Authorization": f"Bearer {sa()}"}) is None


# --- BOARD_SA_MAP: a service account's ID token that carries no email (MS-656) ---

SA_ID = "112233445566778899001"
MAPPED = "overlord@board.example"


def bare_sa(sub=SA_ID, aud=SA_AUD, key=GOOGLE_KEY, iss="https://accounts.google.com", **kw):
    """An ID token minted without include_email: Google's claims carry sub, no email."""
    return sign(key, "RS256", iss=iss, aud=aud, sub=sub, azp=sub, **kw)


MAPPED_ENV = FULL | {auth.SA_MAP_ENV: f" {SA_ID} = {MAPPED.upper()} ",
                     auth.ALLOWED_ENV: f"{CHAOS}, {BOT}, {MAPPED}"}


def test_an_emailless_token_is_refused_without_a_map():
    assert who(FULL, {"Authorization": f"Bearer {bare_sa()}"}) is None


def test_a_mapped_service_account_id_acts_as_its_mapped_email():
    got = who(MAPPED_ENV, {"Authorization": f"Bearer {bare_sa()}"})
    assert got == auth.Identity(MAPPED, "agent")


def test_the_allowlist_still_gates_the_mapped_email():
    env = MAPPED_ENV | {auth.ALLOWED_ENV: f"{CHAOS}, {BOT}"}
    assert who(env, {"Authorization": f"Bearer {bare_sa()}"}) is None


def test_an_unmapped_id_is_refused():
    assert who(MAPPED_ENV, {"Authorization": f"Bearer {bare_sa(sub='999')}"}) is None


def test_a_mapped_id_still_needs_a_valid_google_signature_and_audience():
    for token in (bare_sa(key=ROGUE_KEY), bare_sa(aud="https://other"),
                  bare_sa(iss="https://evil.test")):
        assert who(MAPPED_ENV, {"Authorization": f"Bearer {token}"}) is None


def test_the_map_applies_only_to_service_account_id_tokens():
    assert who(MAPPED_ENV, {auth.IAP_HEADER: iap(sub=SA_ID, email="x@evil.test")}) is None


def test_the_map_wins_over_an_email_in_the_same_token():
    token = sa(sub=SA_ID)
    assert who(MAPPED_ENV, {"Authorization": f"Bearer {token}"}) == auth.Identity(MAPPED, "agent")


def test_an_unmapped_token_with_an_email_behaves_as_before():
    assert who(MAPPED_ENV, {"Authorization": f"Bearer {sa(sub='999')}"}) == auth.Identity(BOT, "agent")


@pytest.mark.parametrize("bad", ["justanid", f"{SA_ID}=", f"={MAPPED}",
                                 f"{SA_ID}=a@b.c,{SA_ID}=d@e.f", f"{SA_ID}=not-an-email",
                                 f"{BOT}={MAPPED}", f"accounts.google.com:{SA_ID}={MAPPED}",
                                 f"{SA_ID}=a@", f"{SA_ID}=a@b@c", f"{SA_ID}=a b@c"])
def test_a_malformed_map_refuses_to_start(bad):
    with pytest.raises(ValueError):
        authn(FULL | {auth.SA_MAP_ENV: bad})


def test_a_map_without_a_service_account_audience_refuses_to_start():
    with pytest.raises(ValueError):
        authn({auth.SA_MAP_ENV: f"{SA_ID}={MAPPED}", auth.ALLOWED_ENV: MAPPED})


def test_the_map_is_ignored_on_the_cloudflare_path_and_by_sub_on_access_tokens():
    assert who(MAPPED_ENV, {auth.CF_HEADER: cf(sub=SA_ID, email="x@evil.test")}) is None
    TOKENINFO["mapped-sub-token"] = info(email="x@evil.test") | {"sub": SA_ID}
    assert who(MAPPED_ENV, {"Authorization": "Bearer mapped-sub-token"}) is None


# --- BOARD_SA_MAP on the access-token path: tokeninfo's azp, no email (MS-658) ---


def sa_info(azp=SA_ID, expires_in="3599", **kw):
    """Tokeninfo for a service account's access token minted without userinfo.email."""
    return {"azp": azp, "aud": azp, "expires_in": expires_in, "scope": "openid"} | kw


TOKENINFO.update({"sa-access-token": sa_info(),
                  "sa-expired-token": sa_info(expires_in="0"),
                  "sa-unmapped-token": sa_info(azp="999"),
                  "sa-aud-only-token": sa_info(azp="555") | {"aud": SA_ID},
                  "sa-with-email-token": sa_info(email=BOT, email_verified="true")})

ACCESS_MAPPED_ENV = {auth.ALLOWED_ENV: f"{CHAOS}, {MAPPED}",
                     auth.CLIENT_IDS_ENV: f"{CLIENT}, {SA_ID}, 999, 555",
                     auth.SA_MAP_ENV: f"{SA_ID}={MAPPED}"}


def test_a_mapped_azp_in_the_client_ids_acts_as_its_mapped_email():
    got = who(ACCESS_MAPPED_ENV, {"Authorization": "Bearer sa-access-token"})
    assert got == auth.Identity(MAPPED, "agent")


def test_a_mapped_azp_not_in_the_client_ids_is_refused():
    env = ACCESS_MAPPED_ENV | {auth.CLIENT_IDS_ENV: CLIENT, auth.SA_AUDIENCE_ENV: SA_AUD}
    assert who(env, {"Authorization": "Bearer sa-access-token"}) is None


def test_an_unmapped_azp_without_an_email_is_refused():
    assert who(ACCESS_MAPPED_ENV, {"Authorization": "Bearer sa-unmapped-token"}) is None


def test_the_access_map_keys_on_azp_never_aud():
    assert who(ACCESS_MAPPED_ENV, {"Authorization": "Bearer sa-aud-only-token"}) is None


def test_the_allowlist_still_gates_an_access_mapped_email():
    env = ACCESS_MAPPED_ENV | {auth.ALLOWED_ENV: CHAOS}
    assert who(env, {"Authorization": "Bearer sa-access-token"}) is None


def test_an_expired_mapped_access_token_is_refused():
    assert who(ACCESS_MAPPED_ENV, {"Authorization": "Bearer sa-expired-token"}) is None


def test_the_access_map_wins_over_an_email_in_the_same_token():
    got = who(ACCESS_MAPPED_ENV, {"Authorization": "Bearer sa-with-email-token"})
    assert got == auth.Identity(MAPPED, "agent")


def test_a_human_access_token_behaves_as_before_with_a_map_set():
    got = who(ACCESS_MAPPED_ENV, {"Authorization": "Bearer good-token"})
    assert got == auth.Identity(CHAOS, "human")


def test_a_map_with_only_client_ids_starts():
    a = authn(ACCESS_MAPPED_ENV)
    assert a.sa_map == {SA_ID: MAPPED}


def test_a_map_whose_ids_are_not_client_ids_refuses_to_start_without_an_audience():
    with pytest.raises(ValueError):
        authn(ACCESS_MAPPED_ENV | {auth.CLIENT_IDS_ENV: CLIENT})


# A service account's access token can carry two dots, like a JWT (ya29.c.<...>).
# identify must not stop at the ID-token path for it.

TOKENINFO.update({"ya29.c.sa-access": sa_info(), "ya29.c.human-access": info()})


def test_a_two_dot_service_account_access_token_falls_back_to_tokeninfo():
    got = who(ACCESS_MAPPED_ENV, {"Authorization": "Bearer ya29.c.sa-access"})
    assert got == auth.Identity(MAPPED, "agent")


def test_a_two_dot_access_token_falls_back_with_a_service_account_audience_set():
    env = ACCESS_MAPPED_ENV | {auth.SA_AUDIENCE_ENV: SA_AUD}
    got = who(env, {"Authorization": "Bearer ya29.c.sa-access"})
    assert got == auth.Identity(MAPPED, "agent")


def test_a_two_dot_human_access_token_falls_back_to_tokeninfo():
    got = who(FULL, {"Authorization": "Bearer ya29.c.human-access"})
    assert got == auth.Identity(CHAOS, "human")


def test_a_two_dot_token_neither_path_accepts_is_refused():
    # Regression guard: refused before the fallback too.
    assert who(FULL, {"Authorization": "Bearer ya29.c.unknown"}) is None


# Google's tokeninfo for an ID token carries exp, never expires_in.
TOKENINFO["id.token.foreign"] = {"azp": SA_ID, "aud": "someone-else", "exp": "9999999999"}


def test_an_id_token_the_jwt_path_refuses_is_not_admitted_by_tokeninfo():
    env = ACCESS_MAPPED_ENV | {auth.SA_AUDIENCE_ENV: SA_AUD}
    assert who(env, {"Authorization": "Bearer id.token.foreign"}) is None

# --- the allowlist and the modes -------------------------------------------------


def test_the_allowlist_gates_every_credential():
    stranger = "x@evil.test"
    assert who(FULL, {auth.IAP_HEADER: iap(email=stranger)}) is None
    assert who(FULL, {auth.CF_HEADER: cf(email=stranger)}) is None
    assert who(FULL, {"Authorization": "Bearer stranger-token"}) is None
    assert who(FULL, {"Authorization": f"Bearer {sa(email=stranger)}"}) is None


def test_the_allowlist_ignores_case():
    assert who(FULL, {auth.IAP_HEADER: iap(email=CHAOS.upper())}) == auth.Identity(CHAOS, "human")


def test_a_verifier_without_an_allowlist_refuses_everyone():
    env = {k: v for k, v in FULL.items() if k != auth.ALLOWED_ENV}
    assert who(env, {auth.IAP_HEADER: iap()}) is None


@pytest.mark.parametrize("partial", [{auth.CF_TEAM_ENV: CF_TEAM}, {auth.CF_AUD_ENV: CF_AUD},
                                     {auth.CLIENT_IDS_ENV: CLIENT}])
def test_a_half_configured_verifier_is_verified_mode_and_refuses(partial):
    a = authn(partial)
    assert a.verified
    assert a.identify({auth.CF_HEADER: cf(), web.ACTOR_HEADER: CHAOS}) is None


def test_no_verifier_configured_is_local_mode():
    a = authn({})
    assert not a.verified
    assert a.identify({}) is None


# --- through the web app ---------------------------------------------------------


@pytest.fixture
def board(migrated):
    core.create_project(migrated, "MS", "memory-solution")
    return migrated


def verified_client(board, monkeypatch):
    monkeypatch.delenv(web.PORT_ENV, raising=False)
    monkeypatch.delenv(web.HOSTS_ENV, raising=False)
    monkeypatch.setenv(web.ACTOR_ENV, "local@example.com")
    app = web.create_app(board, authenticator=authn(FULL))
    return TestClient(app, base_url=LOCAL, follow_redirects=False)


def ready(engine):
    return core.create(engine, "MS", "item", actor=CHAOS, actor_kind="human",
                       state="ready")["id"]


def test_a_verified_assertion_writes_as_its_email(board, monkeypatch):
    client = verified_client(board, monkeypatch)
    iid = ready(board)
    r = client.post(f"/issues/{iid}/note", data={"note": "via IAP"},
                    headers={auth.IAP_HEADER: iap()})
    assert r.status_code == 303
    last = core.show(board, iid)["events"][-1]
    assert (last["actor"], last["actor_kind"]) == (CHAOS, "human")


def test_a_service_account_writes_as_an_agent(board, monkeypatch):
    client = verified_client(board, monkeypatch)
    iid = ready(board)
    r = client.post(f"/issues/{iid}/note", data={"note": "from a bot"},
                    headers={"Authorization": f"Bearer {sa()}"})
    assert r.status_code == 303
    last = core.show(board, iid)["events"][-1]
    assert (last["actor"], last["actor_kind"]) == (BOT, "agent")


def test_a_mapped_service_account_writes_as_its_mapped_email(board, monkeypatch):
    monkeypatch.delenv(web.PORT_ENV, raising=False)
    monkeypatch.delenv(web.HOSTS_ENV, raising=False)
    app = web.create_app(board, authenticator=authn(MAPPED_ENV))
    client = TestClient(app, base_url=LOCAL, follow_redirects=False)
    iid = ready(board)
    r = client.post(f"/issues/{iid}/note", data={"note": "from a bare token"},
                    headers={"Authorization": f"Bearer {bare_sa()}"})
    assert r.status_code == 303
    last = core.show(board, iid)["events"][-1]
    assert (last["actor"], last["actor_kind"]) == (MAPPED, "agent")


def test_verified_mode_refuses_the_bare_header_and_board_web_actor(board, monkeypatch):
    client = verified_client(board, monkeypatch)
    iid = ready(board)
    r = client.post(f"/issues/{iid}/note", data={"note": "spoofed"},
                    headers={web.ACTOR_HEADER: CHAOS})
    assert r.status_code == 401
    assert [e["kind"] for e in core.show(board, iid)["events"]] == ["create"]
