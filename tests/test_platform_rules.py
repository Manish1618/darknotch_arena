"""One-step sign-up, password reset by email, the site admin, who may see what,
individual mode, and disqualifying a team."""

import pyotp
import pytest

from app import cli, mailer
from app.config import settings
from app.models import utcnow
from app.routers import auth
from conftest import PW, Client, last_code
from test_challenges import EV, M, attempt, make, player

ALL_PUBLIC = {k: "public" for k in ("scoreboard", "graph", "feed", "challenge_stats", "solvers", "teams", "matrix")}


def signup(email, name="newbie"):
    return Client().c.post("/api/auth/register", json={"email": email, "username": name, "password": PW})


def login(email, password=PW, **kw):
    c = Client()
    r = c.c.post("/api/auth/login", json={"email": email, "password": password, **kw})
    if r.status_code == 200:
        c.csrf = r.json()["csrf"]
    return c, r


def make_admin(email):
    assert cli.main(["make-admin", email]) == 0
    c, r = login(email)
    assert r.status_code == 200
    return c


# --- sign-up: one step, real mail domains only --------------------------------

def test_signup_is_one_step_and_refuses_throwaway_and_taken_addresses():
    assert signup("new@x.io").status_code == 201
    assert signup("new@x.io", name="again").status_code == 409
    r = signup("someone@mailinator.com", name="someone")
    assert r.status_code == 422 and "permanent" in r.json()["detail"]


def test_new_accounts_per_network_are_capped(monkeypatch):
    monkeypatch.setattr(auth, "SIGNUPS_PER_IP_HOUR", 2)
    assert signup("one@x.io", name="one").status_code == 201
    assert signup("two@x.io", name="two").status_code == 201
    assert signup("three@x.io", name="three").status_code == 429


def test_password_reset_ends_every_session_and_keeps_2fa():
    c = Client()
    c.register("rita")
    secret = c.post("/api/auth/2fa/setup").json()["secret"]
    c.post("/api/auth/2fa/enable", {"code": pyotp.TOTP(secret).now()})
    stranger = Client().c.post("/api/auth/password/forgot", json={"email": "nobody@x.io"})
    r = Client().c.post("/api/auth/password/forgot", json={"email": "rita@x.io"})
    assert r.json() == stranger.json()
    assert not any(m["to"] == "nobody@x.io" for m in mailer.OUTBOX)
    new = "a-brand-new-passphrase"
    bad = Client().c.post("/api/auth/password/reset", json={"email": "rita@x.io", "code": "999999", "password": new})
    assert bad.status_code == 400
    ok = Client().c.post("/api/auth/password/reset", json={"email": "rita@x.io", "code": last_code("rita@x.io"), "password": new})
    assert ok.status_code == 200
    assert c.get("/api/auth/me").status_code == 401                    # old session is gone
    assert login("rita@x.io", new)[1].status_code == 401               # 2FA still required
    assert login("rita@x.io", new, totp=pyotp.TOTP(secret).at(utcnow().timestamp() + 30))[1].status_code == 200


# --- site admin --------------------------------------------------------------

def test_admin_pages_are_invisible_to_everyone_else():
    c = Client()
    c.register("plain")
    for url in ("/api/admin/overview", "/api/admin/users", "/api/admin/settings", "/api/admin/audit"):
        assert c.get(url).status_code == 404
    assert Client().get("/api/admin/users").status_code == 401


def test_admin_disables_and_restores_an_account():
    Client().register("boss")
    victim = Client()
    victim.register("mallory")
    admin = make_admin("boss@x.io")
    users = admin.get("/api/admin/users?q=mall").json()["users"]
    uid = users[0]["id"]
    assert admin.post(f"/api/admin/users/{uid}/disable").status_code == 200
    assert victim.get("/api/auth/me").status_code == 401
    assert login("mallory@x.io")[1].status_code == 401
    assert admin.post(f"/api/admin/users/{uid}/enable").status_code == 200
    assert login("mallory@x.io")[1].status_code == 200
    me = admin.get("/api/auth/me").json()["user"]
    assert me["is_admin"] and admin.post(f"/api/admin/users/{me['id']}/disable").status_code == 403
    log = [a["action"] for a in admin.get("/api/admin/audit").json()]
    assert "admin_user_disabled" in log and "admin_user_enabled" in log


def test_admin_reset_2fa_needs_the_admins_own_code():
    Client().register("chief")
    u = Client()
    u.register("lostphone")
    s = u.post("/api/auth/2fa/setup").json()["secret"]
    u.post("/api/auth/2fa/enable", {"code": pyotp.TOTP(s).now()})
    admin = make_admin("chief@x.io")
    a_secret = admin.post("/api/auth/2fa/setup").json()["secret"]
    r = admin.post("/api/auth/2fa/enable", {"code": pyotp.TOTP(a_secret).now()})
    admin.csrf = r.json()["csrf"]
    uid = admin.get("/api/admin/users?q=lostphone").json()["users"][0]["id"]
    assert admin.post(f"/api/admin/users/{uid}/reset-2fa", {"code": ""}).status_code == 400
    code = pyotp.TOTP(a_secret).at(utcnow().timestamp() + 30)
    assert admin.post(f"/api/admin/users/{uid}/reset-2fa", {"code": code}).status_code == 200
    assert login("lostphone@x.io")[1].status_code == 200               # password alone again


def test_admin_signup_rules():
    Client().register("ruler")
    admin = make_admin("ruler@x.io")
    rules = {"registration_open": True, "block_disposable": True, "allowed_domains": ["college.edu"]}
    assert admin.put("/api/admin/settings", rules).json()["allowed_domains"] == ["college.edu"]
    assert signup("out@x.io", name="out").status_code == 422
    assert signup("in@cs.college.edu", name="inside").status_code == 201
    assert admin.put("/api/admin/settings", {**rules, "registration_open": False}).status_code == 200
    assert signup("in2@college.edu", name="inside2").status_code == 403
    assert admin.put("/api/admin/settings", {**rules, "allowed_domains": ["not a domain"]}).status_code == 422


# --- who may see what ---------------------------------------------------------

def test_visibility_is_enforced_by_the_api(org_with_event):
    o = org_with_event
    cid = make(o, slug="web1", flag="f1")
    a = player("alice")
    attempt(a, cid, "f1")
    anon = Client()
    # the solve matrix starts organisers-only
    assert anon.get(EV + "/scoreboard/matrix").status_code == 403
    assert a.get(EV + "/scoreboard/matrix").status_code == 403
    assert o.get(EV + "/scoreboard/matrix").status_code == 200
    assert a.get(f"{EV}/challenges/{cid}/solves").status_code == 200   # players see solvers by default
    rules = {**ALL_PUBLIC, "scoreboard": "players", "feed": "organisers", "challenge_stats": "organisers",
             "solvers": "organisers", "teams": "organisers", "graph": "organisers"}
    assert o.put(M + "/visibility", rules).status_code == 200
    assert anon.get(EV + "/scoreboard").status_code == 403
    board = a.get(EV + "/scoreboard").json()
    assert board["standings"] and board["feed"] == [] and board["board"] == [] and board["buckets"] == []
    assert o.get(EV + "/scoreboard").json()["feed"]                    # organisers still see it all
    assert a.get(EV + "/scoreboard/graph").status_code == 403
    assert a.get(f"{EV}/challenges/{cid}/solves").status_code == 403
    assert a.get(EV + "/challenges").json()[0]["solves"] is None
    assert anon.get(EV + "/teams").status_code == 403
    assert a.get("/api/events/fall-ctf").json()["can_see"]["matrix"] is True
    assert o.put(M + "/visibility", {**rules, "matrix": "everyone"}).status_code == 422
    assert a.put(M + "/visibility", ALL_PUBLIC).status_code == 404     # players can't change it


# --- individual mode and disqualification -------------------------------------

def test_individual_mode(org_with_event):
    o = org_with_event
    a = player("alice")
    b = player("bobby", code=a.code)
    assert o.patch(EV, {"team_mode": "solo"}).status_code == 409       # a team already has two
    b.post(EV + "/teams/leave")
    ev = o.patch(EV, {"team_mode": "solo"}).json()
    assert ev["team_mode"] == "solo" and ev["max_team_size"] == 1
    c = Client()
    c.register("carol")
    assert c.post(EV + "/teams", {"name": "carol"}).status_code == 201
    assert "join_code" not in c.get(EV + "/teams/me").json()
    assert b.post(EV + "/teams/join", {"code": a.code}).status_code == 400


def test_disqualified_team_leaves_the_board_and_cant_play(org_with_event):
    o = org_with_event
    c1, c2 = make(o, slug="web1", flag="f1"), make(o, slug="web2", flag="f2")
    a, b = player("alice"), player("bobby")
    attempt(a, c1, "f1")
    attempt(b, c1, "f1")
    teams = {t["name"]: t for t in o.get(M + "/teams").json()}
    tid = teams["team alice"]["id"]
    assert o.post(f"{M}/teams/{tid}/ban", {"reason": "Shared flags in chat"}).status_code == 200
    rows = Client().get(EV + "/scoreboard").json()["standings"]
    assert [r["team"] for r in rows] == ["team bobby"] and rows[0]["first_bloods"] == 1
    r = attempt(a, c2, "f2")
    assert r.status_code == 403 and "Shared flags" in r.json()["detail"]
    assert any(n["kind"] == "disqualified" for n in a.get(EV + "/notices?after=0").json()["notices"])
    assert o.post(f"{M}/teams/{tid}/unban").status_code == 200
    assert {r["team"] for r in Client().get(EV + "/scoreboard").json()["standings"]} == {"team alice", "team bobby"}


def test_organiser_removes_a_member(org_with_event):
    o = org_with_event
    a = player("alice")
    b = player("bobby", code=a.code)
    t = next(t for t in o.get(M + "/teams").json() if t["name"] == "team alice")
    bob = next(m for m in t["member_ids"] if m["username"] == "bobby")
    assert o.delete(f"{M}/teams/{t['id']}/members/{bob['id']}").status_code == 200
    assert b.get(EV + "/teams/me").status_code == 404
    assert a.get(EV + "/teams/me").json()["members"] == ["alice"]


def test_log_mail_mode_writes_codes_to_the_log_and_never_connects(monkeypatch):
    def no_network(*a, **k):
        raise AssertionError("SMTP_HOST=log must not open a connection")
    monkeypatch.setattr(settings, "smtp_host", "log")
    monkeypatch.setattr(settings, "cookie_secure", True)
    monkeypatch.setattr(mailer.smtplib, "SMTP", no_network)
    monkeypatch.setattr(mailer.smtplib, "SMTP_SSL", no_network)
    mailer.code_mail("logged@x.io", "register", "123456")
    assert mailer.OUTBOX[-1]["to"] == "logged@x.io" and "123456" in mailer.OUTBOX[-1]["text"]
    monkeypatch.setattr(settings, "smtp_host", "")
    with pytest.raises(mailer.MailError):  # a real server with no mail at all still refuses
        mailer.send("logged@x.io", "s", "t")
