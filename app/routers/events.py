import secrets
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import visibility
from ..audit import audit, changes
from ..db import get_db
from ..deps import current_user, get_visible_event, optional_user, org_role, require_organiser_mfa
from ..models import Event, Organization, Team, TeamMember, User, aware
from ..schemas import EventPatch, JoinIn, TeamIn

router = APIRouter(prefix="/api/events", tags=["events"])


def _public(ev: Event, org: Organization) -> dict:
    return {"slug": ev.slug, "title": ev.title, "description": ev.description,
            "status": ev.status, "org": {"name": org.name, "slug": org.slug},
            "starts_at": aware(ev.starts_at), "ends_at": aware(ev.ends_at),
            "registration_open": ev.registration_open, "max_team_size": ev.max_team_size,
            "paused": ev.paused, "freeze_at": aware(ev.freeze_at), "first_blood_bonus": ev.first_blood_bonus,
            "lockout_threshold": ev.lockout_threshold, "lockout_minutes": ev.lockout_minutes,
            "wrong_penalty": ev.wrong_penalty, "team_mode": ev.team_mode,
            "server_time": datetime.now(timezone.utc).timestamp()}


@router.get("")
def list_events(db: Session = Depends(get_db)):
    rows = (db.query(Event, Organization).join(Organization, Organization.id == Event.org_id)
              .filter(Event.status == "published").order_by(Event.starts_at))
    return [_public(e, o) for e, o in rows]


@router.get("/{slug}")
def get_event(slug: str, user: User | None = Depends(optional_user), db: Session = Depends(get_db)):
    ev = get_visible_event(db, slug, user)
    out = _public(ev, db.get(Organization, ev.org_id))
    out["is_organiser"] = bool(org_role(db, ev.org_id, user))
    out["can_see"] = visibility.can_see(db, ev, user)
    if out["is_organiser"]:
        out["visibility"] = visibility.rules(ev)
    return out


@router.patch("/{slug}")
def update_event(slug: str, body: EventPatch, request: Request, user: User = Depends(current_user),
                 db: Session = Depends(get_db)):
    ev = get_visible_event(db, slug, user)
    if not org_role(db, ev.org_id, user):
        raise HTTPException(404, "Event not found")
    require_organiser_mfa(user)
    data = body.model_dump(exclude_unset=True)
    if data.get("team_mode") == "solo" and ev.team_mode != "solo":
        crowded = (db.query(TeamMember.team_id).filter_by(event_id=ev.id)
                     .group_by(TeamMember.team_id).having(func.count(TeamMember.id) > 1).first())
        if crowded:
            raise HTTPException(409, "Some teams already have more than one member. "
                                     "Individual mode needs every team to be a single player.")
    what = changes(ev, data)
    for k, v in data.items():
        setattr(ev, k, v)
    if ev.team_mode == "solo":
        ev.max_team_size = 1
    if ev.starts_at and ev.ends_at and aware(ev.ends_at) <= aware(ev.starts_at):
        db.rollback()
        raise HTTPException(422, "End time must be after start time")
    if what:
        audit(db, "event_settings", user.id, ev.id, what, request)
    db.commit()
    return _public(ev, db.get(Organization, ev.org_id))


# --- teams -----------------------------------------------------------------

def _registrable(db: Session, slug: str, user: User) -> Event:
    ev = get_visible_event(db, slug, user)
    if ev.status != "published" or not ev.registration_open:
        raise HTTPException(400, "Registration is closed for this event")
    if ev.ends_at and aware(ev.ends_at) < datetime.now(timezone.utc):
        raise HTTPException(400, "This event has ended")
    if org_role(db, ev.org_id, user):
        raise HTTPException(400, "Organisers can't play in their own event")
    return ev


def _my_membership(db: Session, ev: Event, user: User) -> TeamMember | None:
    return db.query(TeamMember).filter_by(event_id=ev.id, user_id=user.id).first()


def _team_view(db: Session, team: Team, include_code: bool) -> dict:
    members = (db.query(User.username).join(TeamMember, TeamMember.user_id == User.id)
                 .filter(TeamMember.team_id == team.id).all())
    out = {"id": team.id, "name": team.name, "members": [m[0] for m in members],
           "banned": team.banned, "ban_reason": team.ban_reason if team.banned else ""}
    ev = db.get(Event, team.event_id)
    if include_code and ev.team_mode == "team":
        out["join_code"] = team.join_code
    return out


@router.post("/{slug}/teams", status_code=201)
def create_team(slug: str, body: TeamIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    ev = _registrable(db, slug, user)
    if _my_membership(db, ev, user):
        raise HTTPException(409, "You're already in a team for this event")
    team = Team(event_id=ev.id, name=body.name.strip(), join_code=secrets.token_urlsafe(12),
                captain_id=user.id)
    db.add(team)
    try:
        db.flush()
        db.add(TeamMember(team_id=team.id, event_id=ev.id, user_id=user.id))
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "Team name taken, or you already joined a team")
    return _team_view(db, team, include_code=True)


@router.post("/{slug}/teams/join")
def join_team(slug: str, body: JoinIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    ev = _registrable(db, slug, user)
    if ev.team_mode == "solo":
        raise HTTPException(400, "This event is for individual players, so there are no teams to join")
    if _my_membership(db, ev, user):
        raise HTTPException(409, "You're already in a team for this event")
    team = db.query(Team).filter_by(event_id=ev.id, join_code=body.code.strip()).first()
    if not team:
        raise HTTPException(404, "Invalid team code")
    # Lock the team row so two people can't both take the last slot (Postgres).
    db.query(Team).filter_by(id=team.id).with_for_update().first()
    size = db.query(func.count(TeamMember.id)).filter_by(team_id=team.id).scalar()
    if team.banned:
        raise HTTPException(403, "That team was disqualified by the organisers")
    if size >= ev.max_team_size:
        raise HTTPException(409, "That team is full")
    db.add(TeamMember(team_id=team.id, event_id=ev.id, user_id=user.id))
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "You're already in a team for this event")
    return _team_view(db, team, include_code=True)


@router.get("/{slug}/teams/me")
def my_team(slug: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    ev = get_visible_event(db, slug, user)
    m = _my_membership(db, ev, user)
    if not m:
        raise HTTPException(404, "You're not in a team for this event")
    return _team_view(db, db.get(Team, m.team_id), include_code=True)


@router.post("/{slug}/teams/leave")
def leave_team(slug: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    ev = get_visible_event(db, slug, user)
    m = _my_membership(db, ev, user)
    if not m:
        raise HTTPException(404, "You're not in a team for this event")
    team = db.get(Team, m.team_id)
    db.delete(m)
    db.flush()
    rest = db.query(TeamMember).filter_by(team_id=team.id).order_by(TeamMember.id).all()
    if not rest:
        db.delete(team)
    elif team.captain_id == user.id:
        team.captain_id = rest[0].user_id
    db.commit()
    return {"ok": True}


@router.get("/{slug}/teams")
def list_teams(slug: str, user: User | None = Depends(optional_user), db: Session = Depends(get_db)):
    ev = get_visible_event(db, slug, user)
    visibility.require(db, ev, user, "teams")
    rows = (db.query(Team.name, func.count(TeamMember.id))
              .outerjoin(TeamMember, TeamMember.team_id == Team.id)
              .filter(Team.event_id == ev.id).group_by(Team.id).order_by(Team.name))
    return [{"name": n, "size": c} for n, c in rows]
