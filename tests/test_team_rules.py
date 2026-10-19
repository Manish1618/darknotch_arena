"""Wrong-flag lockout, wrong-flag penalties, organiser point changes,
announcements and the team notices that tell teammates about all of it."""
from datetime import timedelta

from conftest import Client
from app.db import SessionLocal
from app.models import Lockout, utcnow
from test_challenges import EV, M, attempt, make, player


def notices(p, after=0):
    r = p.get(f"{EV}/notices?after={after}")
    assert r.status_code == 200, r.text
    return r.json()["notices"]


def score_of(viewer, team):
    rows = viewer.get(EV + "/scoreboard").json()["standings"]
    return next((r["score"] for r in rows if r["team"] == team), None)


def test_teammates_get_notices_other_teams_dont(org_with_event):
    o = org_with_event
    cid = make(o, slug="web1", value=200)
    a = player("alice", team="Null Byte")
    b = player("bob", code=a.code)
    eve = player("eve", team="Others")
    start = b.get(EV + "/notices").json()
    assert start["notices"] == [] and start["last"] == 0
    assert attempt(a, cid, "FLAG{secret_one}").json()["first_blood"]
    got = notices(b)
    assert [(n["kind"], n["actor"], n["subject"], n["points"]) for n in got] == [("first_blood", "alice", "Web1", 200)]
    assert notices(eve) == []
    assert notices(b, after=got[-1]["id"]) == []


def test_lockout_after_too_many_wrong_flags(org_with_event):
    o = org_with_event
    o.patch(EV, {"lockout_threshold": 3, "lockout_minutes": 2})
    cid = make(o, slug="web1")
    other = make(o, slug="web2", flag="FLAG{two}")
    a = player("alice", team="Null Byte")
    b = player("bob", code=a.code)
    assert attempt(a, cid, "nope1").json()["locked_for"] is None
    assert attempt(b, cid, "nope2").json()["locked_for"] is None
    r = attempt(a, cid, "nope3").json()          # third wrong flag for the team
    assert r["locked_for"] == 120 and "locked out" in r["message"]
    r = attempt(b, cid, "FLAG{secret_one}")      # even the right flag waits
    assert r.status_code == 429 and "locked out" in r.json()["detail"]
    assert attempt(a, other, "FLAG{two}").json()["correct"]   # other challenges still open
    assert [n["kind"] for n in notices(b)] == ["lockout", "first_blood"]
    assert o.get(M + "/lockouts").json()[0]["team"] == "Null Byte"

    # once it expires the count starts again from zero
    db = SessionLocal()
    db.query(Lockout).update({"until": utcnow() - timedelta(seconds=1)})
    db.commit()
    assert attempt(a, cid, "nope4").json()["locked_for"] is None
    assert attempt(a, cid, "FLAG{secret_one}").json()["correct"]


def test_organiser_can_lift_a_lockout(org_with_event):
    o = org_with_event
    o.patch(EV, {"lockout_threshold": 1, "lockout_minutes": 30})
    cid = make(o, slug="web1")
    a = player("alice")
    attempt(a, cid, "nope")
    assert attempt(a, cid, "FLAG{secret_one}").status_code == 429
    lid = o.get(M + "/lockouts").json()[0]["id"]
    assert o.delete(f"{M}/lockouts/{lid}").status_code == 200
    assert attempt(a, cid, "FLAG{secret_one}").json()["correct"]


def test_wrong_flag_penalty_costs_points(org_with_event):
    o = org_with_event
    o.patch(EV, {"wrong_penalty": 15})
    cid = make(o, slug="web1", value=100)
    a = player("alice", team="Null Byte")
    b = player("bob", code=a.code)
    r = attempt(a, cid, "nope").json()
    assert r["penalty"] == 15 and "-15" in r["message"]
    assert attempt(a, cid, "FLAG{secret_one}").json()["correct"]
    assert score_of(o, "Null Byte") == 85
    assert [(n["kind"], n["points"]) for n in notices(b)] == [("penalty", -15), ("first_blood", 100)]
    # later rule changes don't rewrite history
    o.patch(EV, {"wrong_penalty": 0})
    assert score_of(o, "Null Byte") == 85


def test_organiser_point_changes_reach_the_team(org_with_event):
    o = org_with_event
    cid = make(o, slug="web1", value=100)
    a = player("alice", team="Null Byte")
    attempt(a, cid, "FLAG{secret_one}")
    tid = next(t["id"] for t in o.get(M + "/teams").json() if t["name"] == "Null Byte")
    r = o.post(M + "/awards", {"team_id": tid, "points": -40, "reason": "Shared a flag in public chat"})
    assert r.status_code == 201, r.text
    assert score_of(o, "Null Byte") == 60
    got = notices(a)
    assert (got[-1]["kind"], got[-1]["points"], got[-1]["text"]) == ("deduction", -40, "Shared a flag in public chat")
    assert o.post(M + "/awards", {"team_id": tid, "points": 0, "reason": "nothing"}).status_code == 422
    aid = o.get(M + "/awards").json()[0]["id"]
    assert o.delete(f"{M}/awards/{aid}").status_code == 200
    assert score_of(o, "Null Byte") == 100

    # a team with only a bonus still shows up on the board
    z = player("zed", team="Late Team")
    zid = next(t["id"] for t in o.get(M + "/teams").json() if t["name"] == "Late Team")
    o.post(M + "/awards", {"team_id": zid, "points": 10, "reason": "Best write-up"})
    assert score_of(z, "Late Team") == 10


def test_players_cant_touch_organiser_tools(org_with_event):
    o = org_with_event
    a = player("alice", team="Null Byte")
    tid = o.get(M + "/teams").json()[0]["id"]
    assert a.post(M + "/awards", {"team_id": tid, "points": 500, "reason": "free points"}).status_code == 404
    assert a.post(M + "/announcements", {"text": "fake news"}).status_code == 404
    assert a.get(M + "/teams").status_code == 404


def test_announcements_reach_everyone(org_with_event):
    o = org_with_event
    a = player("alice", team="Null Byte")
    b = player("bob", team="Bit Flippers")
    r = o.post(M + "/announcements", {"text": "Challenge web1 was fixed, try again"})
    assert r.status_code == 201
    for p in (a, b, o):
        got = notices(p)
        assert [(n["kind"], n["team"], n["text"]) for n in got] == [("announcement", False, "Challenge web1 was fixed, try again")]
    assert Client().get(EV + "/announcements").json()[0]["text"].startswith("Challenge web1")
    assert o.delete(f"{M}/announcements/{r.json()['id']}").status_code == 200
    assert notices(a) == []


def test_hint_unlock_notifies_team(org_with_event):
    o = org_with_event
    cid = make(o, slug="web1", value=100, hints=[{"content": "look closer", "cost": 30}])
    other = make(o, slug="web2", value=100, flag="FLAG{two}")
    a = player("alice", team="Null Byte")
    b = player("bob", code=a.code)
    attempt(a, other, "FLAG{two}")
    hid = a.get(f"{EV}/challenges/{cid}").json()["hints"][0]["id"]
    assert a.post(f"{EV}/hints/{hid}/unlock").status_code == 200
    assert [(n["kind"], n["subject"], n["points"]) for n in notices(b)][-1] == ("hint", "Web1", -30)
