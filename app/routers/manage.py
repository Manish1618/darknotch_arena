"""Organiser side: build and run an event's challenges from the browser.
Every route requires owner/admin of the event's organisation; others get 404."""
import os
import re
import secrets
from datetime import timedelta
from hashlib import sha256 as _sha256

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import visibility
from ..audit import audit, changes
from ..config import settings
from ..db import get_db
from ..deps import current_user, get_visible_event, org_role, require_organiser_mfa
from ..models import (Attachment, AuditLog, Award, Challenge, Event, Flag, Hint, Lockout, Notice,
                      Solve, Submission, Team, TeamMember, User, aware, utcnow)
from ..notices import notify, view as notice_view
from ..schemas import (AnnouncementIn, AwardIn, BanIn, ChallengeIn, ChallengePatch, FlagIn, HintIn, HintPatch,
                       VisibilityIn)
from ..scoring import challenge_value, flag_digest, solve_counts, standings

router = APIRouter(prefix="/api/events/{slug}/manage", tags=["manage"])

SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]")


def _event(db: Session, slug: str, user: User) -> Event:
    ev = get_visible_event(db, slug, user)
    if not org_role(db, ev.org_id, user):
        raise HTTPException(404, "Event not found")
    require_organiser_mfa(user)
    return ev


def _challenge(db: Session, ev: Event, cid: int) -> Challenge:
    ch = db.get(Challenge, cid)
    if not ch or ch.event_id != ev.id:
        raise HTTPException(404, "Challenge not found")
    return ch


def _check_requires(db: Session, ev: Event, slugs: list[str], own: str):
    if own in slugs:
        raise HTTPException(422, "A challenge can't require itself")
    known = {s for (s,) in db.query(Challenge.slug).filter_by(event_id=ev.id)}
    missing = sorted(set(slugs) - known)
    if missing:
        raise HTTPException(422, f"Unknown required challenges: {', '.join(missing)}")


def _view(ch: Challenge, solves: int) -> dict:
    return {"id": ch.id, "slug": ch.slug, "name": ch.name, "category": ch.category,
            "description": ch.description, "value": ch.value, "current_value": challenge_value(ch, solves),
            "scoring": ch.scoring, "minimum": ch.minimum, "decay": ch.decay, "state": ch.state,
            "max_attempts": ch.max_attempts, "requires": ch.requires or [], "solves": solves,
            # flags are write-only: organisers see how many, never the text
            "flags": [{"id": f.id, "case_insensitive": f.case_insensitive} for f in ch.flags],
            "hints": [{"id": h.id, "content": h.content, "cost": h.cost} for h in ch.hints],
            "files": [{"id": a.id, "filename": a.filename, "size": a.size, "sha256": a.sha256} for a in ch.files]}


@router.get("/challenges")
def list_all(slug: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    ev = _event(db, slug, user)
    counts = solve_counts(db, ev)
    return [_view(c, counts.get(c.id, 0))
            for c in db.query(Challenge).filter_by(event_id=ev.id).order_by(Challenge.category, Challenge.id)]


@router.post("/challenges", status_code=201)
def create(slug: str, request: Request, body: ChallengeIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    ev = _event(db, slug, user)
    _check_requires(db, ev, body.requires, body.slug)
    ch = Challenge(event_id=ev.id, **body.model_dump(exclude={"flags", "hints"}))
    ch.flags = [Flag(digest=flag_digest(f.flag, f.case_insensitive), case_insensitive=f.case_insensitive)
                for f in body.flags]
    ch.hints = [Hint(content=h.content, cost=h.cost) for h in body.hints]
    db.add(ch)
    audit(db, "challenge_created", user.id, ev.id, body.slug, request)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "A challenge with that slug already exists in this event")
    return _view(ch, 0)


@router.patch("/challenges/{cid}")
def update(slug: str, request: Request, cid: int, body: ChallengePatch, user: User = Depends(current_user),
           db: Session = Depends(get_db)):
    ev = _event(db, slug, user)
    ch = _challenge(db, ev, cid)
    data = body.model_dump(exclude_unset=True)
    if "requires" in data:
        _check_requires(db, ev, data["requires"], ch.slug)
    what = changes(ch, data)
    for k, v in data.items():
        setattr(ch, k, v)
    if ch.scoring == "dynamic" and (ch.minimum is None or not ch.decay or ch.minimum > ch.value):
        db.rollback()
        raise HTTPException(422, "Dynamic scoring needs a minimum at or below the value, and a decay")
    if what:
        audit(db, "challenge_updated", user.id, ev.id, f"{ch.slug}: {what}", request)
    db.commit()
    return _view(ch, solve_counts(db, ev).get(ch.id, 0))


@router.delete("/challenges/{cid}")
def delete(slug: str, request: Request, cid: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    ev = _event(db, slug, user)
    ch = _challenge(db, ev, cid)
    stored = [a.stored_as for a in ch.files]
    # anything that required this challenge would otherwise stay locked forever
    for other in db.query(Challenge).filter(Challenge.event_id == ev.id, Challenge.id != ch.id):
        if ch.slug in (other.requires or []):
            other.requires = [r for r in other.requires if r != ch.slug]
    audit(db, "challenge_deleted", user.id, ev.id, ch.slug, request)
    db.delete(ch)
    db.commit()
    for name in stored:
        _remove_file(name)
    return {"ok": True}


# --- flags -----------------------------------------------------------------

@router.post("/challenges/{cid}/flags", status_code=201)
def add_flag(slug: str, request: Request, cid: int, body: FlagIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    ev = _event(db, slug, user)
    ch = _challenge(db, ev, cid)
    if len(ch.flags) >= 20:
        raise HTTPException(422, "A challenge can have at most 20 flags")
    f = Flag(challenge_id=ch.id, digest=flag_digest(body.flag, body.case_insensitive),
             case_insensitive=body.case_insensitive)
    db.add(f)
    audit(db, "flag_added", user.id, ev.id, ch.slug, request)
    db.commit()
    return {"id": f.id, "case_insensitive": f.case_insensitive}


@router.delete("/challenges/{cid}/flags/{fid}")
def delete_flag(slug: str, request: Request, cid: int, fid: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    ev = _event(db, slug, user)
    ch = _challenge(db, ev, cid)
    f = next((f for f in ch.flags if f.id == fid), None)
    if not f:
        raise HTTPException(404, "Flag not found")
    if len(ch.flags) == 1:
        raise HTTPException(422, "Add another flag before removing the last one")
    db.delete(f)
    audit(db, "flag_removed", user.id, ev.id, ch.slug, request)
    db.commit()
    return {"ok": True}


# --- hints -----------------------------------------------------------------

@router.post("/challenges/{cid}/hints", status_code=201)
def add_hint(slug: str, request: Request, cid: int, body: HintIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    ev = _event(db, slug, user)
    ch = _challenge(db, ev, cid)
    if len(ch.hints) >= 10:
        raise HTTPException(422, "A challenge can have at most 10 hints")
    h = Hint(challenge_id=ch.id, content=body.content, cost=body.cost)
    db.add(h)
    audit(db, "hint_added", user.id, ev.id, ch.slug, request)
    db.commit()
    return {"id": h.id, "content": h.content, "cost": h.cost}


def _hint(db: Session, ev: Event, hid: int) -> Hint:
    h = db.get(Hint, hid)
    if not h or _challenge(db, ev, h.challenge_id) is None:
        raise HTTPException(404, "Hint not found")
    return h


@router.patch("/hints/{hid}")
def update_hint(slug: str, request: Request, hid: int, body: HintPatch, user: User = Depends(current_user), db: Session = Depends(get_db)):
    ev = _event(db, slug, user)
    h = _hint(db, ev, hid)
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(h, k, v)
    audit(db, "hint_updated", user.id, ev.id, f"hint {h.id}", request)
    db.commit()
    return {"id": h.id, "content": h.content, "cost": h.cost}


@router.delete("/hints/{hid}")
def delete_hint(slug: str, request: Request, hid: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    ev = _event(db, slug, user)
    h = _hint(db, ev, hid)
    db.delete(h)
    audit(db, "hint_deleted", user.id, ev.id, f"hint {hid}", request)
    db.commit()
    return {"ok": True}


# --- files -----------------------------------------------------------------

def _path(stored_as: str) -> str:
    return os.path.join(settings.upload_dir, stored_as)


def _remove_file(stored_as: str):
    try:
        os.remove(_path(stored_as))
    except FileNotFoundError:
        pass


@router.post("/challenges/{cid}/files", status_code=201)
async def upload(slug: str, request: Request, cid: int, file: UploadFile = File(...),
                 user: User = Depends(current_user), db: Session = Depends(get_db)):
    ev = _event(db, slug, user)
    ch = _challenge(db, ev, cid)
    name = SAFE_NAME.sub("_", os.path.basename(file.filename or "file"))[:120].lstrip(".") or "file"
    stored = secrets.token_hex(16)
    os.makedirs(settings.upload_dir, exist_ok=True)
    digest, size = _sha256(), 0
    try:
        with open(_path(stored), "xb") as out:
            while chunk := await file.read(1 << 20):
                size += len(chunk)
                if size > settings.max_upload_bytes:
                    raise HTTPException(413, f"File is over {settings.max_upload_bytes // (1024 * 1024)} MB")
                digest.update(chunk)
                out.write(chunk)
    except HTTPException:
        _remove_file(stored)
        raise
    a = Attachment(challenge_id=ch.id, filename=name, stored_as=stored, size=size, sha256=digest.hexdigest())
    db.add(a)
    audit(db, "file_uploaded", user.id, ev.id, f"{ch.slug}: {name} ({size} bytes)", request)
    db.commit()
    return {"id": a.id, "filename": a.filename, "size": a.size, "sha256": a.sha256}


@router.delete("/files/{fid}")
def delete_file(slug: str, request: Request, fid: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    ev = _event(db, slug, user)
    a = db.get(Attachment, fid)
    if not a:
        raise HTTPException(404, "File not found")
    _challenge(db, ev, a.challenge_id)
    stored = a.stored_as
    db.delete(a)
    audit(db, "file_deleted", user.id, ev.id, a.filename, request)
    db.commit()
    _remove_file(stored)
    return {"ok": True}


# --- live monitoring -------------------------------------------------------

@router.get("/submissions")
def submissions(slug: str, limit: int = Query(100, ge=1, le=500), only_wrong: bool = False,
                user: User = Depends(current_user), db: Session = Depends(get_db)):
    ev = _event(db, slug, user)
    q = (db.query(Submission, Team.name, User.username, Challenge.name)
           .join(Team, Team.id == Submission.team_id)
           .join(User, User.id == Submission.user_id)
           .join(Challenge, Challenge.id == Submission.challenge_id)
           .filter(Submission.event_id == ev.id))
    if only_wrong:
        q = q.filter(Submission.correct.is_(False))
    rows = q.order_by(Submission.id.desc()).limit(limit)
    return [{"at": aware(s.created).isoformat(), "team": t, "user": u, "challenge": c,
             "correct": s.correct, "provided": s.provided, "penalty": s.penalty, "ip": s.ip}
            for s, t, u, c in rows]


@router.get("/stats")
def stats(slug: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    ev = _event(db, slug, user)
    recent = utcnow() - timedelta(minutes=10)
    total = db.query(func.count(Submission.id)).filter_by(event_id=ev.id).scalar()
    right = db.query(func.count(Solve.id)).filter_by(event_id=ev.id).scalar()
    teams = db.query(func.count(Team.id)).filter_by(event_id=ev.id).scalar()
    return {"teams": teams, "submissions": total, "solves": right, "wrong": total - right,
            "players": db.query(func.count(TeamMember.id)).filter_by(event_id=ev.id).scalar(),
            "disqualified": db.query(func.count(Team.id)).filter_by(event_id=ev.id, banned=True).scalar(),
            "solves_10m": db.query(func.count(Solve.id)).filter(Solve.event_id == ev.id, Solve.created >= recent).scalar(),
            "wrong_10m": db.query(func.count(Submission.id)).filter(
                Submission.event_id == ev.id, Submission.correct.is_(False), Submission.created >= recent).scalar(),
            "locked_now": db.query(func.count(Lockout.id)).filter(Lockout.event_id == ev.id, Lockout.until > utcnow()).scalar()}


# --- who may see what --------------------------------------------------------

@router.put("/visibility")
def set_visibility(slug: str, request: Request, body: VisibilityIn, user: User = Depends(current_user),
                   db: Session = Depends(get_db)):
    """Choose, part by part, what players and the public may see. Enforced by
    the API itself, not just hidden in the page."""
    ev = _event(db, slug, user)
    before, after = visibility.rules(ev), body.model_dump()
    what = ", ".join(f"{k} {before[k]} -> {v}" for k, v in after.items() if before[k] != v)
    ev.visibility = after
    if what:
        audit(db, "visibility_changed", user.id, ev.id, what, request)
    db.commit()
    return visibility.rules(ev)


# --- teams, point adjustments, lockouts, announcements ---------------------

@router.get("/teams")
def teams(slug: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Every team with its members and live score (organisers ignore the freeze)."""
    ev = _event(db, slug, user)
    score = {r["team_id"]: r for r in standings(db, ev)}
    members: dict[int, list[dict]] = {}
    for tid, uid, name in (db.query(TeamMember.team_id, User.id, User.username).join(User, User.id == TeamMember.user_id)
                             .filter(TeamMember.event_id == ev.id).order_by(User.username)):
        members.setdefault(tid, []).append({"id": uid, "username": name})
    out = [{"id": t.id, "name": t.name, "members": [m["username"] for m in members.get(t.id, [])],
            "member_ids": members.get(t.id, []), "captain_id": t.captain_id,
            "banned": t.banned, "ban_reason": t.ban_reason, "created": aware(t.created).isoformat(),
            "score": score.get(t.id, {}).get("score", 0), "rank": score.get(t.id, {}).get("rank"),
            "solves": score.get(t.id, {}).get("solves", 0)}
           for t in db.query(Team).filter_by(event_id=ev.id).order_by(Team.name)]
    return out


def _team(db: Session, ev: Event, tid: int) -> Team:
    t = db.get(Team, tid)
    if not t or t.event_id != ev.id:
        raise HTTPException(404, "Team not found")
    return t


@router.post("/teams/{tid}/ban")
def ban_team(slug: str, tid: int, body: BanIn, request: Request, user: User = Depends(current_user),
             db: Session = Depends(get_db)):
    """Disqualify: off the board, no more flags or hints, and the team is told why."""
    ev = _event(db, slug, user)
    t = _team(db, ev, tid)
    t.banned, t.ban_reason = True, body.reason.strip()
    notify(db, ev, "disqualified", t.id, "organisers", text=t.ban_reason)
    audit(db, "team_disqualified", user.id, ev.id, f"{t.name}: {t.ban_reason}", request)
    db.commit()
    return {"ok": True}


@router.post("/teams/{tid}/unban")
def unban_team(slug: str, tid: int, request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    ev = _event(db, slug, user)
    t = _team(db, ev, tid)
    t.banned, t.ban_reason = False, ""
    notify(db, ev, "reinstated", t.id, "organisers")
    audit(db, "team_reinstated", user.id, ev.id, t.name, request)
    db.commit()
    return {"ok": True}


@router.delete("/teams/{tid}/members/{uid}")
def remove_member(slug: str, tid: int, uid: int, request: Request, user: User = Depends(current_user),
                  db: Session = Depends(get_db)):
    """Take a player out of a team (wrong team, account sharing). Their solves
    stay with the team; a team left empty is removed."""
    ev = _event(db, slug, user)
    t = _team(db, ev, tid)
    m = db.query(TeamMember).filter_by(team_id=t.id, user_id=uid).first()
    if not m:
        raise HTTPException(404, "That player isn't in this team")
    name = db.get(User, uid).username
    db.delete(m)
    db.flush()
    rest = db.query(TeamMember).filter_by(team_id=t.id).order_by(TeamMember.id).all()
    if not rest:
        audit(db, "team_member_removed", user.id, ev.id, f"{name} from {t.name} (team removed, now empty)", request)
        db.delete(t)
    else:
        if t.captain_id == uid:
            t.captain_id = rest[0].user_id
        notify(db, ev, "member_removed", t.id, "organisers", text=name)
        audit(db, "team_member_removed", user.id, ev.id, f"{name} from {t.name}", request)
    db.commit()
    return {"ok": True}


@router.get("/awards")
def list_awards(slug: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    ev = _event(db, slug, user)
    rows = (db.query(Award, Team.name).join(Team, Team.id == Award.team_id)
              .filter(Award.event_id == ev.id).order_by(Award.id.desc()).limit(200))
    return [{"id": a.id, "team": n, "points": a.points, "reason": a.reason,
             "at": aware(a.created).isoformat()} for a, n in rows]


@router.post("/awards", status_code=201)
def add_award(slug: str, request: Request, body: AwardIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Add or deduct points by hand. The team sees the change and the reason."""
    ev = _event(db, slug, user)
    t = _team(db, ev, body.team_id)
    a = Award(event_id=ev.id, team_id=t.id, points=body.points, reason=body.reason.strip(), created_by=user.id)
    db.add(a)
    notify(db, ev, "award" if body.points > 0 else "deduction", t.id, "organisers", "", body.points, a.reason)
    audit(db, "points_adjusted", user.id, ev.id, f"{t.name}: {body.points:+d} ({a.reason})", request)
    db.commit()
    return {"id": a.id, "team": t.name, "points": a.points, "reason": a.reason}


@router.delete("/awards/{aid}")
def delete_award(slug: str, request: Request, aid: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    ev = _event(db, slug, user)
    a = db.get(Award, aid)
    if not a or a.event_id != ev.id:
        raise HTTPException(404, "Adjustment not found")
    db.delete(a)
    audit(db, "points_adjustment_undone", user.id, ev.id, f"{a.points:+d} ({a.reason})", request)
    db.commit()
    return {"ok": True}


@router.get("/lockouts")
def lockouts(slug: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    ev = _event(db, slug, user)
    rows = (db.query(Lockout, Team.name, Challenge.name)
              .join(Team, Team.id == Lockout.team_id).join(Challenge, Challenge.id == Lockout.challenge_id)
              .filter(Lockout.event_id == ev.id, Lockout.until > utcnow()).order_by(Lockout.until))
    return [{"id": l.id, "team": t, "challenge": c, "until": aware(l.until).isoformat()} for l, t, c in rows]


@router.delete("/lockouts/{lid}")
def lift_lockout(slug: str, request: Request, lid: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    ev = _event(db, slug, user)
    l = db.get(Lockout, lid)
    if not l or l.event_id != ev.id:
        raise HTTPException(404, "Lockout not found")
    l.until = utcnow()
    audit(db, "lockout_lifted", user.id, ev.id, f"lockout {l.id}", request)
    db.commit()
    return {"ok": True}


@router.post("/announcements", status_code=201)
def announce(slug: str, request: Request, body: AnnouncementIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """A pop-up for everyone viewing the event."""
    ev = _event(db, slug, user)
    n = notify(db, ev, "announcement", None, "organisers", text=body.text.strip())
    audit(db, "announcement", user.id, ev.id, body.text.strip()[:200], request)
    db.commit()
    return notice_view(n)


@router.delete("/announcements/{nid}")
def delete_announcement(slug: str, request: Request, nid: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    ev = _event(db, slug, user)
    n = db.get(Notice, nid)
    if not n or n.event_id != ev.id or n.team_id is not None:
        raise HTTPException(404, "Announcement not found")
    db.delete(n)
    audit(db, "announcement_deleted", user.id, ev.id, n.text[:200], request)
    db.commit()
    return {"ok": True}


@router.get("/audit")
def audit_log(slug: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Who changed what in this event, newest first."""
    ev = _event(db, slug, user)
    rows = (db.query(AuditLog, User.username).outerjoin(User, User.id == AuditLog.user_id)
              .filter(AuditLog.event_id == ev.id).order_by(AuditLog.id.desc()).limit(300))
    return [{"at": aware(a.created).isoformat(), "user": u or "?", "action": a.action,
             "detail": a.detail, "ip": a.ip} for a, u in rows]
