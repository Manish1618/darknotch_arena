"""Sign in with Google, and the Redis-backed state that lets several API
containers run side by side."""
import time
from urllib.parse import parse_qs, urlsplit

import fakeredis
import jwt
import pyotp
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from app import boardcache, oidc, shared
from app.config import settings
from app.security import Limiter
from conftest import PW, Client, last_code
from test_challenges import EV, attempt, make, player


# --- Sign in with Google ----------------------------------------------------

@pytest.fixture
def google(monkeypatch):
    monkeypatch.setattr(settings, "google_client_id", "client-123")
    monkeypatch.setattr(settings, "google_client_secret", "secret-456")
    who = {"sub": "g-1", "email": "gina@college.edu", "name": "Gina"}
    monkeypatch.setattr(oidc, "fetch_identity", lambda provider, code, verifier, nonce: dict(who))
    return who


def round_trip(c, nxt="/e/fall-ctf"):
    r = c.c.get(f"/api/auth/oauth/google/start?next={nxt}", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("https://accounts.google.com/")
    q = parse_qs(urlsplit(r.headers["location"]).query)
    assert q["code_challenge_method"] == ["S256"] and q["nonce"] and q["state"]
    return q["state"][0], c.c.get(f"/api/auth/oauth/google/callback?code=abc&state={q['state'][0]}", follow_redirects=False)


def test_google_sign_in_creates_a_verified_account_once(google):
    c = Client()
    assert Client().get("/api/site").json()["providers"] == [{"id": "google", "name": "Google"}]
    state, r = round_trip(c)
    assert r.status_code == 303 and r.headers["location"] == "/e/fall-ctf"
    me = c.get("/api/auth/me").json()["user"]
    assert me["email"] == "gina@college.edu" and me["username"] == "gina"
    # the state works once, and only in the browser that started it
    again = c.c.get(f"/api/auth/oauth/google/callback?code=abc&state={state}", follow_redirects=False)
    assert again.headers["location"].startswith("/login?error=")
    other = Client()
    s2 = parse_qs(urlsplit(c.c.get("/api/auth/oauth/google/start", follow_redirects=False).headers["location"]).query)["state"][0]
    stolen = other.c.get(f"/api/auth/oauth/google/callback?code=abc&state={s2}", follow_redirects=False)
    assert stolen.headers["location"].startswith("/login?error=")
    # signing in again finds the same account through the provider's id
    _, r = round_trip(Client())
    assert r.status_code == 303


def test_google_links_an_existing_account_but_still_asks_for_2fa(google):
    c = Client()
    c.register("gina", "gina@college.edu")
    # the owner has proven the inbox once (a reset by emailed code)
    Client().c.post("/api/auth/password/forgot", json={"email": "gina@college.edu"})
    assert Client().c.post("/api/auth/password/reset", json={"email": "gina@college.edu", "code": last_code("gina@college.edu"),
                                                            "password": PW + "-new"}).status_code == 200
    c, r = Client(), Client().c.post("/api/auth/login", json={"email": "gina@college.edu", "password": PW + "-new"})
    c.c.cookies = r.cookies
    c.csrf = r.json()["csrf"]
    secret = c.post("/api/auth/2fa/setup").json()["secret"]
    c.post("/api/auth/2fa/enable", {"code": pyotp.TOTP(secret).now()})
    b = Client()
    _, r = round_trip(b)
    assert r.headers["location"].startswith("/login#ticket=")
    assert b.get("/api/auth/me").status_code == 401                    # not signed in yet
    ticket = r.headers["location"].split("ticket=")[1]
    assert b.c.post("/api/auth/oauth/finish", json={"ticket": ticket, "totp": "000000"}).status_code == 401
    ok = b.c.post("/api/auth/oauth/finish", json={"ticket": ticket, "totp": pyotp.TOTP(secret).at(time.time() + 30)})
    assert ok.status_code == 200 and ok.json()["next"] == "/e/fall-ctf"


def test_squatter_who_signed_up_with_someone_elses_email_is_locked_out(google):
    squatter = Client()
    squatter.register("squat", "gina@college.edu")
    secret = squatter.post("/api/auth/2fa/setup").json()["secret"]
    squatter.post("/api/auth/2fa/enable", {"code": pyotp.TOTP(secret).now()})
    owner = Client()
    _, r = round_trip(owner)
    assert r.status_code == 303 and r.headers["location"] == "/e/fall-ctf"   # no squatter's 2FA in the way
    assert owner.get("/api/auth/me").json()["user"]["totp_enabled"] is False
    assert squatter.get("/api/auth/me").status_code == 401                  # squatter's session ended
    for totp in (None, pyotp.TOTP(secret).at(time.time() + 30)):
        body = {"email": "gina@college.edu", "password": PW, **({"totp": totp} if totp else {})}
        assert Client().c.post("/api/auth/login", json=body).status_code == 401  # their password is gone


def test_google_sign_up_follows_the_site_rules_and_stays_on_site(google):
    google["email"] = "x@mailinator.com"
    _, r = round_trip(Client())
    assert r.headers["location"].startswith("/login?error=")
    google["email"] = "fine@college.edu"
    _, r = round_trip(Client(), nxt="//evil.example/steal")
    assert r.headers["location"] == "/"


def test_unconfigured_provider_is_404():
    assert Client().c.get("/api/auth/oauth/github/start", follow_redirects=False).status_code == 404


# --- the ID token checks ------------------------------------------------------

class _FakeKeys:
    def __init__(self, pub):
        self.pub = pub

    def get_signing_key_from_jwt(self, _token):
        return type("K", (), {"key": self.pub})()


@pytest.fixture
def signer(monkeypatch):
    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    monkeypatch.setitem(oidc._jwks, "google", _FakeKeys(priv.public_key()))
    monkeypatch.setattr(settings, "google_client_id", "aud-g")
    monkeypatch.setattr(settings, "google_client_secret", "s")
    now = int(time.time())

    def make_token(**claims):
        base = {"iss": "https://accounts.google.com", "aud": "aud-g", "sub": "1", "iat": now, "exp": now + 300,
                "nonce": "n1", "email": "A@B.EDU", "email_verified": True}
        return jwt.encode({**base, **claims}, priv, algorithm="RS256")
    return make_token


def test_id_token_checks(signer):
    assert oidc.verify_id_token("google", signer(), "n1")["email"] == "a@b.edu"
    for bad in ({"nonce": "other"}, {"email_verified": False}, {"aud": "someone-else"},
                {"iss": "https://evil.example"}, {"exp": int(time.time()) - 3600}):
        with pytest.raises(oidc.ProviderError):
            oidc.verify_id_token("google", signer(**bad), "n1")


# --- several API containers sharing Redis ------------------------------------

@pytest.fixture
def redis_shared(monkeypatch):
    r = fakeredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(shared, "redis_client", lambda: r)
    return r


def test_rate_limits_are_shared_between_containers(redis_shared):
    api1, api2 = Limiter(), Limiter()  # two processes, one Redis
    for _ in range(3):
        api1.fail("acct:x", 60)
    assert api2.blocked("acct:x", 3, 60)
    api2.clear("acct:x")
    assert not api1.blocked("acct:x", 3, 60)


def test_login_lockout_holds_across_containers(redis_shared):
    Client().register("frank")
    for _ in range(8):
        Client().c.post("/api/auth/login", json={"email": "frank@x.io", "password": "nope-nope-nope"})
    assert redis_shared.zcard("rl:acct:frank@x.io") == 8
    assert Client().c.post("/api/auth/login", json={"email": "frank@x.io", "password": PW}).status_code == 429


def test_board_cache_is_shared_and_cleared_through_redis(redis_shared, org_with_event, monkeypatch):
    from app.routers import play
    o = org_with_event
    cid = make(o, slug="web1", flag="f1")
    a, b = player("alice"), player("bobby")
    attempt(a, cid, "f1")
    built = []
    real = play.standings
    monkeypatch.setattr(play, "standings", lambda *x, **k: built.append(1) or real(*x, **k))
    Client().get(EV + "/scoreboard")
    boardcache._store.clear()                                          # a different container: nothing local
    Client().get(EV + "/scoreboard")
    assert len(built) == 1                                             # served from Redis
    attempt(b, cid, "f1")                                              # bumps the shared generation
    rows = Client().get(EV + "/scoreboard").json()["standings"]
    assert len(rows) == 2 and len(built) == 2


def test_readiness_reports_a_dead_redis(monkeypatch):
    class Dead:
        def ping(self):
            from redis.exceptions import ConnectionError as RCE
            raise RCE("down")
    monkeypatch.setattr(shared, "redis_client", lambda: Dead())
    assert Client().get("/readyz").status_code == 503
    assert Client().get("/healthz").status_code == 200
