import time
from datetime import datetime, timedelta, timezone

import pytest

from conftest import Client
from app.db import SessionLocal
from app.models import Flag, Submission
from app.scoring import dynamic_value

EV = "/api/events/fall-ctf"
M = EV + "/manage"


def chal(slug, flag="FLAG{secret_one}", **kw):
    return {"slug": slug, "name": slug.title(), "category": "web", "description": "desc",
            "value": 100, "state": "visible", "flags": [{"flag": flag}], **kw}


def player(name, team=None, code=None):
    p = Client()
    p.register(name)
    if code:
        assert p.post(EV + "/teams/join", {"code": code}).status_code == 200
    else:
        r = p.post(EV + "/teams", {"name": team or f"team {name}"})
        assert r.status_code == 201, r.text
        p.code = r.json()["join_code"]
    return p


def make(o, **kw):
    r = o.post(M + "/challenges", chal(**kw))
    assert r.status_code == 201, r.text
    return r.json()["id"]


def attempt(p, cid, flag):
    return p.post(f"{EV}/challenges/{cid}/attempt", {"flag": flag})


def test_flags_are_hashed_and_never_returned(org_with_event):
    o = org_with_event
    cid = make(o, slug="sqli")
    assert "secret_one" not in o.get(M + "/challenges").text
    db = SessionLocal()
    stored = db.query(Flag).one().digest
    assert "secret_one" not in stored and len(stored) == 64
    p = player("alice")
    assert "secret_one" not in p.get(f"{EV}/challenges/{cid}").text


def test_hidden_challenges_not_shown(org_with_event):
    o = org_with_event
    cid = make(o, slug="later", state="hidden")
    p = player("alice")
    assert p.get(EV + "/challenges").json() == []
    assert p.get(f"{EV}/challenges/{cid}").status_code == 404
    assert attempt(p, cid, "FLAG{secret_one}").status_code == 404
    o.patch(f"{M}/challenges/{cid}", {"state": "visible"})
    assert [c["name"] for c in p.get(EV + "/challenges").json()] == ["Later"]


def test_attempt_flow_and_submission_log(org_with_event):
    o = org_with_event
    cid = make(o, slug="sqli")
    a = player("alice")
    b = player("bobby", code=a.code)
    r = attempt(a, cid, "FLAG{nope}").json()
    assert r["status"] == "incorrect"
    r = attempt(a, cid, "  FLAG{secret_one} ").json()   # surrounding spaces are forgiven
    assert r["status"] == "correct" and r["first_blood"] is True
    assert attempt(b, cid, "FLAG{secret_one}").json()["status"] == "already_solved"
    assert b.get(EV + "/challenges").json()[0]["solved"] is True
    db = SessionLocal()
    subs = db.query(Submission).order_by(Submission.id).all()
    assert [(s.correct, s.provided) for s in subs] == [(False, "FLAG{nope}"), (True, "")]
    log = o.get(M + "/submissions").json()
    assert log[0]["correct"] and log[1]["provided"] == "FLAG{nope}"


def test_case_insensitive_and_multiple_flags(org_with_event):
    o = org_with_event
    r = o.post(M + "/challenges", {**chal("multi"), "flags": [{"flag": "FLAG{Mixed}", "case_insensitive": True},
                                                           {"flag": "alt-answer"}]})
    cid = r.json()["id"]
    a, b = player("alice"), player("bobby")
    assert attempt(a, cid, "flag{mixed}").json()["correct"]
    assert attempt(b, cid, "ALT-ANSWER").json()["correct"] is False
    assert attempt(b, cid, "alt-answer").json()["correct"]


def test_first_blood_bonus_and_ranking(org_with_event):
    o = org_with_event
    o.patch(EV, {"first_blood_bonus": 15})
    c1, c2 = make(o, slug="one", flag="f1"), make(o, slug="two", flag="f2")
    a, b, c = player("alice"), player("bobby"), player("carol")
    attempt(b, c1, "f1")          # b: 100 + 15
    attempt(a, c1, "f1")          # a: 100
    attempt(a, c2, "f2")          # a: 200 + 15
    attempt(c, c2, "f2")          # c: 100
    board = Client().get(EV + "/scoreboard").json()
    got = [(r["team"], r["score"], r["first_bloods"]) for r in board["standings"]]
    assert got == [("team alice", 215, 1), ("team bobby", 115, 1), ("team carol", 100, 0)]
    graph = Client().get(EV + "/scoreboard/graph").json()["series"]
    assert graph[0]["team"] == "team alice" and [p[1] for p in graph[0]["points"]] == [100, 215]


def test_ties_go_to_whoever_got_there_first(org_with_event):
    o = org_with_event
    cid = make(o, slug="one", flag="f1")
    a, b = player("alice"), player("bobby")
    attempt(b, cid, "f1")
    attempt(a, cid, "f1")
    assert [r["team"] for r in Client().get(EV + "/scoreboard").json()["standings"]] == ["team bobby", "team alice"]


def test_dynamic_scoring_formula_and_retroactive_decay(org_with_event):
    assert [dynamic_value(500, 100, 3, n) for n in range(0, 6)] == [500, 500, 456, 323, 100, 100]
    o = org_with_event
    cid = make(o, slug="dyn", flag="d", value=500, scoring="dynamic", minimum=100, decay=3)
    teams = [player(n) for n in ("alice", "bobby", "carol")]
    for t in teams:
        attempt(t, cid, "d")
    # every solver gets the current (decayed) value, like CTFd
    assert {r["score"] for r in Client().get(EV + "/scoreboard").json()["standings"]} == {323}
    assert teams[0].get(EV + "/challenges").json()[0]["value"] == 323
    bad = o.post(M + "/challenges", chal("bad", scoring="dynamic", value=100))
    assert bad.status_code == 422


def test_prerequisites(org_with_event):
    o = org_with_event
    c1 = make(o, slug="intro", flag="f1")
    c2 = make(o, slug="next", flag="f2", requires=["intro"])
    assert o.post(M + "/challenges", chal("ghost", requires=["missing"])).status_code == 422
    a = player("alice")
    listed = {c["name"]: c["locked"] for c in a.get(EV + "/challenges").json()}
    assert listed == {"Intro": False, "Next": True}
    assert a.get(f"{EV}/challenges/{c2}").status_code == 403
    assert attempt(a, c2, "f2").status_code == 403
    attempt(a, c1, "f1")
    assert attempt(a, c2, "f2").json()["correct"]


def test_deleting_a_requirement_unlocks_dependents(org_with_event):
    o = org_with_event
    c1 = make(o, slug="intro", flag="f1")
    c2 = make(o, slug="next", flag="f2", requires=["intro"])
    assert o.delete(f"{M}/challenges/{c1}").status_code == 200
    a = player("alice")
    assert attempt(a, c2, "f2").json()["correct"]


def test_max_attempts(org_with_event):
    o = org_with_event
    cid = make(o, slug="tries", flag="f", max_attempts=2)
    a = player("alice")
    assert attempt(a, cid, "x").json()["attempts_left"] == 1
    assert attempt(a, cid, "y").json()["attempts_left"] == 0
    assert attempt(a, cid, "f").status_code == 403


def test_wrong_flag_throttle(org_with_event):
    o = org_with_event
    cid = make(o, slug="brute", flag="f")
    a = player("alice")
    for i in range(10):
        assert attempt(a, cid, f"guess{i}").status_code == 200
    assert attempt(a, cid, "f").status_code == 429


def test_hints_cost_points(org_with_event):
    o = org_with_event
    c1 = make(o, slug="easy", flag="f1", hints=[{"content": "free tip", "cost": 0}])
    c2 = make(o, slug="hard", flag="f2", hints=[{"content": "look at the headers", "cost": 30}])
    a = player("alice")
    hint = a.get(f"{EV}/challenges/{c2}").json()["hints"][0]
    assert hint["content"] is None and hint["unlocked"] is False
    assert a.get(f"{EV}/challenges/{c1}").json()["hints"][0]["content"] == "free tip"
    assert a.post(f"{EV}/hints/{hint['id']}/unlock").status_code == 403   # 0 points
    attempt(a, c1, "f1")
    assert a.post(f"{EV}/hints/{hint['id']}/unlock").json()["content"] == "look at the headers"
    assert a.post(f"{EV}/hints/{hint['id']}/unlock").status_code == 200   # idempotent, charged once
    assert Client().get(EV + "/scoreboard").json()["standings"][0]["score"] == 70


def test_scoreboard_freeze(org_with_event):
    o = org_with_event
    cid = make(o, slug="one", flag="f1")
    a, b = player("alice"), player("bobby")
    attempt(a, cid, "f1")
    o.patch(EV, {"freeze_at": datetime.now(timezone.utc).isoformat()})
    time.sleep(0.05)
    attempt(b, cid, "f1")
    public = Client().get(EV + "/scoreboard").json()
    assert public["frozen"] and [r["team"] for r in public["standings"]] == ["team alice"]
    assert b.get(EV + "/challenges").json()[0]["solves"] == 1     # counts frozen too
    live = o.get(EV + "/scoreboard").json()
    assert not live["frozen"] and len(live["standings"]) == 2


def test_event_window_and_pause(org_with_event):
    o = org_with_event
    cid = make(o, slug="one", flag="f1")
    a = player("alice")
    o.patch(EV, {"paused": True})
    assert attempt(a, cid, "f1").status_code == 403
    o.patch(EV, {"paused": False, "starts_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()})
    assert a.get(EV + "/challenges").status_code == 403
    now = datetime.now(timezone.utc)
    o.patch(EV, {"starts_at": (now - timedelta(hours=2)).isoformat(), "ends_at": (now - timedelta(hours=1)).isoformat()})
    assert a.get(EV + "/challenges").status_code == 200       # can still read after the end
    assert attempt(a, cid, "f1").status_code == 403


def test_player_needs_team_and_organiser_cannot_submit(org_with_event):
    o = org_with_event
    cid = make(o, slug="one", flag="f1")
    loner = Client(); loner.register("loner")
    assert loner.get(EV + "/challenges").status_code == 403
    assert o.get(EV + "/challenges").status_code == 200          # organiser preview
    assert attempt(o, cid, "f1").status_code == 403


def test_manage_routes_are_organiser_only(org_with_event):
    o = org_with_event
    cid = make(o, slug="one", flag="f1")
    a = player("alice")
    rival = Client(); rival.register("rival")
    rival.post("/api/orgs", {"name": "Rival Club", "slug": "rival"})
    for c in (a, rival):
        assert c.get(M + "/challenges").status_code == 404
        assert c.post(M + "/challenges", chal("evil")).status_code == 404
        assert c.patch(f"{M}/challenges/{cid}", {"value": 9999}).status_code == 404
        assert c.delete(f"{M}/challenges/{cid}").status_code == 404
        assert c.get(M + "/submissions").status_code == 404
    assert o.post(f"{M}/challenges/{cid}/flags", {"flag": "f1b"}).status_code == 201


def test_cannot_remove_last_flag(org_with_event):
    o = org_with_event
    cid = make(o, slug="one", flag="f1")
    fid = o.get(M + "/challenges").json()[0]["flags"][0]["id"]
    assert o.delete(f"{M}/challenges/{cid}/flags/{fid}").status_code == 422


def test_file_upload_and_download(org_with_event, monkeypatch):
    o = org_with_event
    cid = make(o, slug="forensics", flag="f1")
    hidden = make(o, slug="secret", flag="f2", state="hidden")
    r = o.upload(f"{M}/challenges/{cid}/files", "../../etc/evil capture.pcap", b"\x00pcapdata")
    assert r.status_code == 201
    f = r.json()
    assert f["filename"] == "evil_capture.pcap" and f["size"] == 9
    h = o.upload(f"{M}/challenges/{hidden}/files", "x.txt", b"hidden").json()
    a = player("alice")
    d = a.get(f"{EV}/files/{f['id']}")
    assert d.status_code == 200 and d.content == b"\x00pcapdata"
    assert d.headers["content-disposition"].startswith("attachment")
    assert d.headers["content-type"] == "application/octet-stream"
    assert a.get(f"{EV}/files/{h['id']}").status_code == 404
    assert Client().get(f"{EV}/files/{f['id']}").status_code == 401
    from app.config import settings
    monkeypatch.setattr(settings, "max_upload_bytes", 4)
    assert o.upload(f"{M}/challenges/{cid}/files", "big.bin", b"12345").status_code == 413
    assert o.delete(f"{M}/files/{f['id']}").status_code == 200
    assert a.get(f"{EV}/files/{f['id']}").status_code == 404
