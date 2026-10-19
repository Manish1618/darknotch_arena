import time

import pyotp

from conftest import PW, Client


def test_register_login_logout():
    c = Client()
    c.register("alice")
    assert c.get("/api/auth/me").json()["user"]["username"] == "alice"
    assert c.post("/api/auth/logout").status_code == 200
    assert c.get("/api/auth/me").status_code == 401
    r = c.c.post("/api/auth/login", json={"email": "ALICE@x.io", "password": PW})
    assert r.status_code == 200


def test_password_is_hashed_and_cookie_httponly():
    c = Client()
    r = c.register("bob")
    assert "httponly" in r.headers["set-cookie"].lower()
    from app.db import SessionLocal
    from app.models import User
    u = SessionLocal().query(User).filter_by(username="bob").one()
    assert u.pw_hash.startswith("$argon2id$") and PW not in u.pw_hash


def test_duplicate_and_weak_password():
    Client().register("carol")
    r = Client().c.post("/api/auth/register", json={"email": "carol@x.io", "username": "carol2", "password": PW})
    assert r.status_code == 409
    r = Client().c.post("/api/auth/register", json={"email": "d@x.io", "username": "dave", "password": "short"})
    assert r.status_code == 422


def test_same_error_for_unknown_email_and_wrong_password():
    Client().register("erin")
    a = Client().c.post("/api/auth/login", json={"email": "erin@x.io", "password": "wrong-password"})
    b = Client().c.post("/api/auth/login", json={"email": "nobody@x.io", "password": "wrong-password"})
    assert a.status_code == b.status_code == 401
    assert a.json() == b.json()


def test_login_throttled_after_repeated_failures():
    Client().register("frank")
    c = Client()
    for _ in range(8):
        assert c.c.post("/api/auth/login", json={"email": "frank@x.io", "password": "nope-nope-nope"}).status_code == 401
    # even the right password is refused while locked
    assert c.c.post("/api/auth/login", json={"email": "frank@x.io", "password": PW}).status_code == 429


def test_csrf_required_on_state_change():
    c = Client()
    c.register("gina")
    assert c.post("/api/orgs", {"name": "Org", "slug": "org-a"}, csrf=False).status_code == 403
    c.csrf = "forged"
    assert c.post("/api/orgs", {"name": "Org", "slug": "org-a"}).status_code == 403


def test_totp_2fa():
    c = Client()
    c.register("hank")
    secret = c.post("/api/auth/2fa/setup").json()["secret"]
    assert c.post("/api/auth/2fa/enable", {"code": "000000"}).status_code == 400
    r = c.post("/api/auth/2fa/enable", {"code": pyotp.TOTP(secret).now()})
    assert r.status_code == 200 and len(r.json()["recovery_codes"]) == 10
    c.csrf = r.json()["csrf"]  # this browser got a fresh session
    assert c.get("/api/auth/me").json()["user"]["totp_enabled"]
    login = lambda **kw: Client().c.post("/api/auth/login", json={"email": "hank@x.io", "password": PW, **kw})
    assert login().status_code == 401
    # the code used to switch 2FA on can't be used again; the next one works
    assert login(totp=pyotp.TOTP(secret).now()).status_code == 401
    assert login(totp=pyotp.TOTP(secret).at(time.time() + 30)).status_code == 200


def test_unknown_fields_rejected():
    r = Client().c.post("/api/auth/register", json={"email": "i@x.io", "username": "ivy", "password": PW, "is_admin": True})
    assert r.status_code == 422


def test_security_headers():
    r = Client().get("/healthz")
    assert r.headers["x-frame-options"] == "DENY"
    assert "default-src 'self'" in r.headers["content-security-policy"]
