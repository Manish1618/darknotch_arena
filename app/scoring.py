"""Flag checking and CTFd-style scoring (dynamic values, first blood, freeze)."""
import hashlib
import hmac
import math
from datetime import datetime, timezone

from sqlalchemy import func
from sqlalchemy.orm import Session

from .config import settings
from .models import (Award, Challenge, Event, Flag, Hint, HintUnlock, Solve, Submission, Team,
                     aware)

_DEV_PEPPER = "dev-only-flag-pepper"


def _pepper() -> bytes:
    return (settings.flag_pepper or _DEV_PEPPER).encode()


def flag_digest(flag: str, case_insensitive: bool) -> str:
    text = flag.strip()
    if case_insensitive:
        text = text.lower()
    return hmac.new(_pepper(), text.encode(), hashlib.sha256).hexdigest()


def check_flag(flags: list[Flag], provided: str) -> bool:
    """Compare against every flag without stopping early."""
    hit = False
    for f in flags:
        hit |= hmac.compare_digest(flag_digest(provided, f.case_insensitive), f.digest)
    return hit


def dynamic_value(initial: int, minimum: int, decay: int, solves: int) -> int:
    """CTFd's formula: the first solver gets full value, then it decays
    quadratically and reaches `minimum` after `decay` solves."""
    n = max(solves - 1, 0)
    v = ((minimum - initial) / (decay ** 2)) * (n ** 2) + initial
    return max(math.ceil(v), minimum)


def challenge_value(ch: Challenge, solves: int) -> int:
    if ch.scoring == "dynamic" and ch.minimum is not None and ch.decay:
        return dynamic_value(ch.value, ch.minimum, ch.decay, solves)
    return ch.value


def freeze_cutoff(ev: Event, is_org: bool) -> datetime | None:
    """The cutoff to apply for this viewer, or None for live data."""
    f = aware(ev.freeze_at)
    if is_org or not f or datetime.now(timezone.utc) < f:
        return None
    return f


def _counted(db: Session, ev: Event, q):
    """Leave out solves by disqualified teams: they don't score, don't take
    first blood and don't lower dynamic values for everyone else (as in CTFd)."""
    banned = [t for (t,) in db.query(Team.id).filter_by(event_id=ev.id, banned=True)]
    return q.filter(Solve.team_id.notin_(banned)) if banned else q


def solve_counts(db: Session, ev: Event, until: datetime | None = None) -> dict[int, int]:
    q = _counted(db, ev, db.query(Solve.challenge_id, func.count(Solve.id)).filter(Solve.event_id == ev.id))
    if until:
        q = q.filter(Solve.created <= until)
    return dict(q.group_by(Solve.challenge_id).all())


def _ledger(db: Session, ev: Event, until: datetime | None):
    """Every scoring event: (time, team_id, points, kind, is_first_blood) with
    kind "solve", "hint" or "penalty" (visible challenges only) or "award"
    (organiser adjustments, positive or negative)."""
    chals = {c.id: c for c in db.query(Challenge).filter_by(event_id=ev.id, state="visible")}
    sq = _counted(db, ev, db.query(Solve).filter(Solve.event_id == ev.id, Solve.challenge_id.in_(chals or [0])))
    if until:
        sq = sq.filter(Solve.created <= until)
    solves = sorted(sq.all(), key=lambda s: (aware(s.created), s.id))
    counts: dict[int, int] = {}
    for s in solves:
        counts[s.challenge_id] = counts.get(s.challenge_id, 0) + 1
    values = {cid: challenge_value(c, counts.get(cid, 0)) for cid, c in chals.items()}

    rows, seen = [], set()
    for s in solves:
        fb = s.challenge_id not in seen
        seen.add(s.challenge_id)
        rows.append((aware(s.created), s.team_id,
                     values[s.challenge_id] + (ev.first_blood_bonus if fb else 0), "solve", fb))
    hq = (db.query(HintUnlock.created, HintUnlock.team_id, Hint.cost)
            .join(Hint, Hint.id == HintUnlock.hint_id)
            .filter(HintUnlock.event_id == ev.id, Hint.challenge_id.in_(chals or [0])))
    if until:
        hq = hq.filter(HintUnlock.created <= until)
    rows += [(aware(t), team, -cost, "hint", False) for t, team, cost in hq if cost]
    pq = (db.query(Submission.created, Submission.team_id, Submission.penalty)
            .filter(Submission.event_id == ev.id, Submission.penalty > 0,
                    Submission.challenge_id.in_(chals or [0])))
    aq = db.query(Award.created, Award.team_id, Award.points).filter(Award.event_id == ev.id)
    if until:
        pq = pq.filter(Submission.created <= until)
        aq = aq.filter(Award.created <= until)
    rows += [(aware(t), team, -p, "penalty", False) for t, team, p in pq]
    rows += [(aware(t), team, pts, "award", False) for t, team, pts in aq]
    rows.sort(key=lambda r: r[0])
    return rows


def standings(db: Session, ev: Event, until: datetime | None = None) -> list[dict]:
    teams = {t.id: t.name for t in db.query(Team).filter_by(event_id=ev.id, banned=False)}
    agg: dict[int, dict] = {}
    for t, team, pts, kind, fb in _ledger(db, ev, until):
        a = agg.setdefault(team, {"score": 0, "solves": 0, "first_bloods": 0, "last": None, "awarded": False})
        a["score"] += pts
        a["awarded"] |= kind == "award"
        if kind == "solve":
            a["solves"] += 1
            a["first_bloods"] += fb
            a["last"] = t  # ties go to whoever reached their score first
    far = datetime.max.replace(tzinfo=timezone.utc)
    # on the board once a team solves something or an organiser adjusts its score
    rows = [{"team_id": tid, "team": teams[tid], **a} for tid, a in agg.items()
            if tid in teams and (a["solves"] or a["awarded"])]
    for r in rows:
        del r["awarded"]
    rows.sort(key=lambda r: (-r["score"], r["last"] or far, r["team_id"]))
    for i, r in enumerate(rows, 1):
        r["rank"] = i
    return rows


def team_score(db: Session, ev: Event, team_id: int) -> int:
    return sum(row[2] for row in _ledger(db, ev, None) if row[1] == team_id)


def score_series(db: Session, ev: Event, top: int, until: datetime | None = None) -> list[dict]:
    """Cumulative score over time for the top teams (the CTFd graph)."""
    ranked = standings(db, ev, until)[:top]
    want = {r["team_id"]: r["team"] for r in ranked}
    lines = {tid: [] for tid in want}
    totals = {tid: 0 for tid in want}
    for t, team, pts, _kind, _fb in _ledger(db, ev, until):
        if team in want:
            totals[team] += pts
            lines[team].append([t.isoformat(), totals[team]])
    return [{"team": want[tid], "points": lines[tid]} for tid in want]


def solve_matrix(db: Session, ev: Event, until: datetime | None) -> dict:
    """Which team solved which challenge, for the scoreboard's matrix view.
    Challenges come in the same order as the board's cards; each team maps to
    [challenge index, solve order] pairs, where order 0 is first blood."""
    chals = sorted(db.query(Challenge).filter_by(event_id=ev.id, state="visible"),
                   key=lambda c: (c.category, c.value, c.id))
    index = {c.id: i for i, c in enumerate(chals)}
    sq = _counted(db, ev, db.query(Solve).filter(Solve.event_id == ev.id, Solve.challenge_id.in_(index or [0])))
    if until:
        sq = sq.filter(Solve.created <= until)
    teams = {t.id: t.name for t in db.query(Team).filter_by(event_id=ev.id, banned=False)}
    counts: dict[int, int] = {}
    solved: dict[str, list[list[int]]] = {}
    for s in sorted(sq.all(), key=lambda s: (aware(s.created), s.id)):
        order = counts.get(s.challenge_id, 0)
        counts[s.challenge_id] = order + 1
        solved.setdefault(teams.get(s.team_id, "?"), []).append([index[s.challenge_id], order])
    return {"challenges": [{"name": c.name, "category": c.category,
                            "value": challenge_value(c, counts.get(c.id, 0)),
                            "solves": counts.get(c.id, 0)} for c in chals],
            "teams": solved}


def board_extras(db: Session, ev: Event, until: datetime | None, minutes: int = 90) -> dict:
    """Public numbers for the themed board once the event is under way:
    per-category and per-challenge solves (with first blood), a recent-captures
    feed, and per-minute activity for the signal strip."""
    chals = {c.id: c for c in db.query(Challenge).filter_by(event_id=ev.id, state="visible")}
    sq = _counted(db, ev, db.query(Solve).filter(Solve.event_id == ev.id, Solve.challenge_id.in_(chals or [0])))
    wq = db.query(Submission.created).filter(Submission.event_id == ev.id, Submission.correct.is_(False))
    if until:
        sq = sq.filter(Solve.created <= until)
        wq = wq.filter(Submission.created <= until)
    solves = sorted(sq.all(), key=lambda s: (aware(s.created), s.id))
    counts: dict[int, int] = {}
    for s in solves:
        counts[s.challenge_id] = counts.get(s.challenge_id, 0) + 1

    cats: dict[str, dict] = {}
    for c in chals.values():
        k = cats.setdefault(c.category, {"category": c.category, "challenges": 0, "solves": 0})
        k["challenges"] += 1
        k["solves"] += counts.get(c.id, 0)

    teams = {t.id: t.name for t in db.query(Team).filter_by(event_id=ev.id, banned=False)}
    first, feed = {}, []
    for s in solves:
        fb = s.challenge_id not in first
        first.setdefault(s.challenge_id, teams.get(s.team_id, "?"))
        c = chals[s.challenge_id]
        feed.append({"team": teams.get(s.team_id, "?"), "category": c.category, "challenge": c.name,
                     "points": challenge_value(c, counts[c.id]), "first_blood": fb,
                     "at": aware(s.created).isoformat()})
    board = [{"name": c.name, "category": c.category, "value": challenge_value(c, counts.get(c.id, 0)),
              "solves": counts.get(c.id, 0), "first_blood": first.get(c.id)}
             for c in sorted(chals.values(), key=lambda c: (c.category, c.value, c.id))]

    end = until or datetime.now(timezone.utc)
    end_min = int(end.timestamp() // 60 * 60)
    start = end_min - (minutes - 1) * 60
    buckets = [{"t": datetime.fromtimestamp(start + i * 60, timezone.utc).isoformat(),
                "correct": 0, "wrong": 0, "fb": 0} for i in range(minutes)]

    def slot(dt):
        i = int((aware(dt).timestamp() - start) // 60)
        return i if 0 <= i < minutes else None

    for f in feed:
        i = slot(datetime.fromisoformat(f["at"]))
        if i is not None:
            buckets[i]["correct"] += 1
            buckets[i]["fb"] += f["first_blood"]
    for (t,) in wq.filter(Submission.created >= datetime.fromtimestamp(start, timezone.utc)):
        i = slot(t)
        if i is not None:
            buckets[i]["wrong"] += 1

    return {"teams": len(teams), "challenges": len(chals), "solves": len(solves),
            "categories": sorted(cats.values(), key=lambda k: k["category"]), "board": board,
            "feed": list(reversed(feed))[:20], "buckets": buckets}
