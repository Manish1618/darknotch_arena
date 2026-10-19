"""Account protection: recovery codes, TOTP replay, password changes,
sessions, organiser 2FA, request limits and the audit trail."""
import time
from datetime import timedelta

import pyotp
import pytest

from conftest import PW, Client
from app.config import settings
from app.db import SessionLocal
from app.models import AuditLog, AuthSession, RecoveryCode, User, utcnow

NEW_PW = "a-much-better-passphrase"


def login(email, password=PW, **kw):
    c = Client()
    r = c.c.post("/api/auth/login", json={"email": email, "password": password, **kw})
    if r.status_code == 200:
        c.csrf = r.json()["csrf"]
    return c, r


def with_2fa(name):
    """A logged-in client with 2FA on. Returns (client, secret, recovery codes)."""
    c = Client()
    c.register(name)
    secret = c.post("/api/auth/2fa/setup").json()["secret"]
    r = c.post("/api/auth/2fa/enable", {"code": pyotp.TOTP(secret).now()})
    assert r.status_code == 200, r.text
    c.csrf = r.json()["csrf"]
    return c, secret, r.json()["recovery_codes"]


def next_code(secret):
    return pyotp.TOTP(secret).at(time.time() + 30)


def actions(user_id):
    db = SessionLocal()
    try:
        return [a.action for a in db.query(AuditLog).filter_by(user_id=user_id).order_by(AuditLog.id)]
    finally:
        db.close()


def test_secret_is_encrypted_at_rest():
    c, secret, _ = with_2fa("alice")
    u = SessionLocal().query(User).filter_by(username="alice").one()
    assert u.totp_secret.startswith("v1:") and secret not in u.totp_secret


def test_recovery_code_works_once():
    _, _, codes = with_2fa("alice")
    c, r = login("alice@x.io", totp=codes[0].upper())   # case and dash don't matter
    assert r.status_code == 200 and r.json()["recovery_codes_left"] == 9
    assert c.get("/api/auth/me").json()["recovery_codes_left"] == 9
    assert login("alice@x.io", totp=codes[0])[1].status_code == 401
    # codes are stored only as keyed hashes
    stored = [rc.digest for rc in SessionLocal().query(RecoveryCode)]
    assert len(stored) == 10 and not any(code.replace("-", "") in d for d in stored for code in codes)


def test_totp_code_cant_be_replayed():
    _, secret, _ = with_2fa("alice")
    code = next_code(secret)
    assert login("alice@x.io", totp=code)[1].status_code == 200
    assert login("alice@x.io", totp=code)[1].status_code == 401


def test_new_recovery_codes_need_an_app_code():
    c, secret, old = with_2fa("alice")
    assert c.post("/api/auth/2fa/recovery-codes", {"code": "123456"}).status_code == 400
    assert c.post("/api/auth/2fa/recovery-codes", {"code": old[1]}).status_code == 400
    r = c.post("/api/auth/2fa/recovery-codes", {"code": next_code(secret)})
    assert r.status_code == 200 and set(r.json()["recovery_codes"]).isdisjoint(old)
    assert login("alice@x.io", totp=old[2])[1].status_code == 401


def test_turning_2fa_off_needs_password_and_code():
    c, secret, codes = with_2fa("alice")
    other, _ = login("alice@x.io", totp=codes[0])
    assert c.post("/api/auth/2fa/disable", {"password": "wrong-password", "code": next_code(secret)}).status_code == 400
    assert c.post("/api/auth/2fa/disable", {"password": PW, "code": "000000"}).status_code == 400
    r = c.post("/api/auth/2fa/disable", {"password": PW, "code": codes[1]})
    assert r.status_code == 200 and not r.json()["user"]["totp_enabled"]
    assert other.get("/api/auth/me").status_code == 401        # other browsers signed out
    assert c.get("/api/auth/me").status_code == 200
    assert login("alice@x.io")[1].status_code == 200


def test_turning_2fa_on_signs_out_other_sessions():
    c = Client()
    c.register("alice")
    other, _ = login("alice@x.io")
    secret = c.post("/api/auth/2fa/setup").json()["secret"]
    r = c.post("/api/auth/2fa/enable", {"code": pyotp.TOTP(secret).now()})
    c.csrf = r.json()["csrf"]
    assert other.get("/api/auth/me").status_code == 401
    assert c.get("/api/auth/me").status_code == 200


def test_password_change():
    c = Client()
    c.register("alice")
    other, _ = login("alice@x.io")
    assert c.post("/api/auth/password", {"current": "wrong-password", "new": NEW_PW}).status_code == 400
    assert c.post("/api/auth/password", {"current": PW, "new": "password123"}).status_code == 422
    assert c.post("/api/auth/password", {"current": PW, "new": NEW_PW}).status_code == 200
    assert other.get("/api/auth/me").status_code == 401
    assert c.get("/api/auth/me").status_code == 200
    assert login("alice@x.io")[1].status_code == 401
    assert login("alice@x.io", NEW_PW)[1].status_code == 200


@pytest.mark.parametrize("pw", ["password123", "qwertyuiop", "aaaaaaaaaaaa", "alice-is-great-99"])
def test_weak_passwords_refused(pw):
    r = Client().c.post("/api/auth/register", json={"email": "alice@x.io", "username": "alice", "password": pw})
    assert r.status_code == 422


def test_sessions_list_and_sign_out_elsewhere():
    c = Client()
    c.register("alice")
    o1, _ = login("alice@x.io")
    o2, _ = login("alice@x.io")
    rows = c.get("/api/auth/sessions").json()
    assert len(rows) == 3 and sum(r["current"] for r in rows) == 1
    one = next(r["id"] for r in rows if not r["current"])
    assert c.delete(f"/api/auth/sessions/{one}").status_code == 200
    assert len(c.get("/api/auth/sessions").json()) == 2
    assert c.post("/api/auth/sessions/revoke-others").json()["ended"] == 1
    assert o1.get("/api/auth/me").status_code == o2.get("/api/auth/me").status_code == 401
    # nobody can end someone else's session
    bob = Client()
    bob.register("bob")
    mine = c.get("/api/auth/sessions").json()[0]["id"]
    assert bob.delete(f"/api/auth/sessions/{mine}").status_code == 404


def test_idle_session_expires():
    c = Client()
    c.register("alice")
    db = SessionLocal()
    db.query(AuthSession).update({"last_seen": utcnow() - timedelta(seconds=settings.session_idle_s + 5)})
    db.commit()
    assert c.get("/api/auth/me").status_code == 401


def test_cross_site_requests_refused():
    c = Client()
    c.register("alice")
    evil = {"X-CSRF-Token": c.csrf, "Origin": "https://evil.example"}
    assert c.c.post("/api/orgs", json={"name": "Org", "slug": "org-a"}, headers=evil).status_code == 403
    r = Client().c.post("/api/auth/login", json={"email": "alice@x.io", "password": PW},
                        headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    ok = {"X-CSRF-Token": c.csrf, "Origin": "http://testserver"}
    assert c.c.post("/api/orgs", json={"name": "Org", "slug": "org-a"}, headers=ok).status_code == 201


def test_organiser_tools_need_2fa_when_required(monkeypatch):
    monkeypatch.setattr(settings, "organiser_mfa", "required")
    c = Client()
    c.register("alice")
    r = c.post("/api/orgs", {"name": "Org", "slug": "org-a"})
    assert r.status_code == 403 and "two-factor" in r.json()["detail"]
    c, _, _ = with_2fa("bob")
    assert c.post("/api/orgs", {"name": "Org", "slug": "org-a"}).status_code == 201
    assert c.post("/api/orgs/org-a/events", {"slug": "ev-1", "title": "Event"}).status_code == 201
    assert Client().get("/api/site").json()["organiser_mfa"] == "required"


def test_oversized_bodies_refused():
    c = Client()
    c.register("alice")
    big = "x" * (settings.max_body_bytes + 10)
    r = c.c.post("/api/orgs", content=f'{{"name": "{big}", "slug": "org-a"}}',
                 headers={"X-CSRF-Token": c.csrf, "Content-Type": "application/json"})
    assert r.status_code == 413

    def chunks():  # no Content-Length: counted as it arrives
        for _ in range(4):
            yield b"x" * (settings.max_body_bytes // 2)
    r = c.c.post("/api/orgs", content=chunks(), headers={"X-CSRF-Token": c.csrf, "Content-Type": "application/json"})
    assert r.status_code == 413


def test_more_security_headers():
    r = Client().get("/api/site")
    assert r.headers["cache-control"] == "no-store"
    assert r.headers["cross-origin-opener-policy"] == "same-origin"
    assert "camera=()" in r.headers["permissions-policy"]


def test_audit_trail(org_with_event):
    o = org_with_event
    uid = SessionLocal().query(User).filter_by(username="organiser").one().id
    login("organiser@x.io", "wrong-password")
    assert actions(uid)[:4] == ["register", "org_created", "event_created", "event_settings"]
    assert actions(uid)[-1] == "login_failed"
    acts = [a["action"] for a in o.get("/api/auth/activity").json()]
    assert "login_failed" in acts and "event_settings" not in acts     # event changes live on the event
    assert o.post("/api/events/fall-ctf/manage/announcements", {"text": "hello"}).status_code == 201
    log = o.get("/api/events/fall-ctf/manage/audit").json()
    assert [a["action"] for a in log][:2] == ["announcement", "event_settings"] and log[0]["user"] == "organiser"
    p = Client()
    p.register("player")
    assert p.get("/api/events/fall-ctf/manage/audit").status_code == 404
