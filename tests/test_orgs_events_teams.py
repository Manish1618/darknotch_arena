from conftest import Client


def test_draft_event_hidden_from_outsiders():
    o = Client()
    o.register("orgone")
    o.post("/api/orgs", {"name": "Org A", "slug": "org-a"})
    o.post("/api/orgs/org-a/events", {"slug": "secret-ctf", "title": "Secret"})
    assert o.get("/api/events/secret-ctf").status_code == 200
    p = Client()
    p.register("player")
    assert p.get("/api/events/secret-ctf").status_code == 404
    assert Client().get("/api/events").json() == []


def test_orgs_are_isolated():
    a, b = Client(), Client()
    a.register("owner_a"); b.register("owner_b")
    a.post("/api/orgs", {"name": "Org A", "slug": "org-a"})
    b.post("/api/orgs", {"name": "Org B", "slug": "org-b"})
    a.post("/api/orgs/org-a/events", {"slug": "a-ctf", "title": "A CTF"})
    # B can't create events in, list, or edit A's org
    assert b.post("/api/orgs/org-a/events", {"slug": "evil", "title": "Evil"}).status_code == 404
    assert b.get("/api/orgs/org-a/events").status_code == 404
    assert b.patch("/api/events/a-ctf", {"title": "pwned"}).status_code == 404
    a.patch("/api/events/a-ctf", {"status": "published"})
    assert b.patch("/api/events/a-ctf", {"title": "pwned"}).status_code == 404


def test_org_admin_can_manage_but_not_add_members():
    o, adm, x = Client(), Client(), Client()
    o.register("owner"); adm.register("helper"); x.register("other")
    o.post("/api/orgs", {"name": "Org A", "slug": "org-a"})
    assert o.post("/api/orgs/org-a/members", {"email": "helper@x.io", "role": "admin"}).status_code == 201
    assert adm.post("/api/orgs/org-a/events", {"slug": "h-ctf", "title": "Helper CTF"}).status_code == 201
    assert adm.post("/api/orgs/org-a/members", {"email": "other@x.io", "role": "admin"}).status_code == 404
    assert o.post("/api/orgs/org-a/members", {"email": "other@x.io", "role": "owner"}).status_code == 422


def test_team_flow(org_with_event):
    p1, p2, p3 = Client(), Client(), Client()
    p1.register("pone"); p2.register("ptwo"); p3.register("pthree")
    t = p1.post("/api/events/fall-ctf/teams", {"name": "Null Byte"})
    assert t.status_code == 201
    code = t.json()["join_code"]
    assert p1.post("/api/events/fall-ctf/teams", {"name": "Second"}).status_code == 409
    assert p2.post("/api/events/fall-ctf/teams/join", {"code": "wrong-code-123"}).status_code == 404
    assert p2.post("/api/events/fall-ctf/teams/join", {"code": code}).status_code == 200
    assert p3.post("/api/events/fall-ctf/teams/join", {"code": code}).status_code == 409  # full (max 2)
    pub = Client().get("/api/events/fall-ctf/teams").json()
    assert pub == [{"name": "Null Byte", "size": 2}]  # no join codes leak
    assert p1.post("/api/events/fall-ctf/teams/leave").status_code == 200
    assert p2.get("/api/events/fall-ctf/teams/me").json()["members"] == ["ptwo"]


def test_registration_closed_and_organiser_cannot_play(org_with_event):
    o = org_with_event
    assert o.post("/api/events/fall-ctf/teams", {"name": "Insiders"}).status_code == 400
    o.patch("/api/events/fall-ctf", {"registration_open": False})
    p = Client(); p.register("late")
    assert p.post("/api/events/fall-ctf/teams", {"name": "Late"}).status_code == 400


def test_event_dates_validated(org_with_event):
    r = org_with_event.patch("/api/events/fall-ctf", {"starts_at": "2026-11-02T10:00:00Z",
                                                      "ends_at": "2026-11-01T10:00:00Z"})
    assert r.status_code == 422


def test_bad_slug_rejected():
    c = Client(); c.register("slugger")
    assert c.post("/api/orgs", {"name": "X", "slug": "../admin"}).status_code == 422
