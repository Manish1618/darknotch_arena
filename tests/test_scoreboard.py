"""Scoreboard views: the solve matrix, and the short cache that lets hundreds
of viewers share one computed board without ever showing a stale capture."""
import time
from datetime import datetime, timezone

from conftest import Client
from app.routers import play
from test_challenges import EV, M, attempt, make, player


def open_to_public(o, **parts):
    """Organisers choose who sees what; these tests want the matrix public."""
    rules = {k: "public" for k in ("scoreboard", "graph", "feed", "challenge_stats", "solvers", "teams", "matrix")}
    r = o.put(M + "/visibility", {**rules, **parts})
    assert r.status_code == 200, r.text


def test_matrix_shows_who_solved_what_in_order(org_with_event):
    o = org_with_event
    c1 = make(o, slug="web1", flag="f1")
    c2 = make(o, slug="web2", flag="f2", value=200)
    a, b = player("alice"), player("bobby")
    attempt(b, c1, "f1")
    attempt(a, c1, "f1")
    attempt(a, c2, "f2")
    open_to_public(o)
    m = Client().get(EV + "/scoreboard/matrix").json()
    assert [(c["name"], c["solves"]) for c in m["challenges"]] == [("Web1", 2), ("Web2", 1)]
    assert m["teams"] == {"team bobby": [[0, 0]], "team alice": [[0, 1], [1, 0]]}


def test_matrix_hides_hidden_challenges_and_respects_freeze(org_with_event):
    o = org_with_event
    cid = make(o, slug="web1", flag="f1")
    make(o, slug="secret", flag="s", state="hidden")
    open_to_public(o)
    a, b = player("alice"), player("bobby")
    attempt(a, cid, "f1")
    o.patch(EV, {"freeze_at": datetime.now(timezone.utc).isoformat()})
    time.sleep(0.05)
    attempt(b, cid, "f1")
    public = Client().get(EV + "/scoreboard/matrix").json()
    assert public["frozen"] and [c["name"] for c in public["challenges"]] == ["Web1"]
    assert public["teams"] == {"team alice": [[0, 0]]}
    live = o.get(EV + "/scoreboard/matrix").json()
    assert not live["frozen"] and set(live["teams"]) == {"team alice", "team bobby"}


def test_matrix_is_empty_before_the_event_starts(org_with_event):
    o = org_with_event
    make(o, slug="web1", flag="f1")
    open_to_public(o)
    o.patch(EV, {"starts_at": "2099-01-01T00:00:00Z"})
    assert Client().get(EV + "/scoreboard/matrix").json()["challenges"] == []
    assert o.get(EV + "/scoreboard/matrix").json()["challenges"] != []   # organisers can preview


def test_board_is_computed_once_for_many_viewers_but_never_stale(org_with_event, monkeypatch):
    o = org_with_event
    cid = make(o, slug="web1", flag="f1")
    a, b = player("alice"), player("bobby")
    attempt(a, cid, "f1")
    built = []
    real = play.standings
    monkeypatch.setattr(play, "standings", lambda *x, **k: built.append(1) or real(*x, **k))
    viewers = [Client() for _ in range(5)]
    boards = [v.get(EV + "/scoreboard").json() for v in viewers]
    assert len(built) == 1                                     # one computation, five viewers
    assert all(bd["standings"] == boards[0]["standings"] for bd in boards)
    attempt(b, cid, "f1")                                      # a capture clears it at once
    assert [r["team"] for r in viewers[0].get(EV + "/scoreboard").json()["standings"]] == ["team alice", "team bobby"]
    assert len(built) == 2


def test_wrong_flag_without_penalty_keeps_the_cache(org_with_event, monkeypatch):
    o = org_with_event
    cid = make(o, slug="web1", flag="f1")
    a = player("alice")
    attempt(a, cid, "f1")
    other = make(o, slug="web2", flag="f2")
    built = []
    real = play.standings
    monkeypatch.setattr(play, "standings", lambda *x, **k: built.append(1) or real(*x, **k))
    Client().get(EV + "/scoreboard")
    attempt(a, other, "nope")                                  # no score change, no rebuild
    Client().get(EV + "/scoreboard")
    assert len(built) == 1


def test_point_changes_show_immediately(org_with_event):
    o = org_with_event
    cid = make(o, slug="web1", flag="f1")
    a = player("alice", team="Null Byte")
    attempt(a, cid, "f1")
    assert Client().get(EV + "/scoreboard").json()["standings"][0]["score"] == 100
    tid = next(t["id"] for t in o.get(M + "/teams").json() if t["name"] == "Null Byte")
    assert o.post(M + "/awards", {"team_id": tid, "points": -30, "reason": "Rule break"}).status_code == 201
    assert Client().get(EV + "/scoreboard").json()["standings"][0]["score"] == 70


def test_graph_carries_event_window(org_with_event):
    o = org_with_event
    o.patch(EV, {"starts_at": "2026-01-01T10:00:00Z", "ends_at": "2026-01-01T18:00:00Z"})
    g = Client().get(EV + "/scoreboard/graph?top=8").json()
    assert g["starts_at"].startswith("2026-01-01T10:00") and g["ends_at"].startswith("2026-01-01T18:00")
