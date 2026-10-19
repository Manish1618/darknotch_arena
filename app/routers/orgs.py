from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..audit import audit
from ..db import get_db
from ..deps import current_user, org_role, require_organiser_mfa
from ..models import Event, Organization, OrgMember, User
from ..schemas import EventIn, MemberIn, OrgIn

router = APIRouter(prefix="/api/orgs", tags=["orgs"])


def _org_for(db: Session, slug: str, user: User, roles=("owner", "admin")) -> Organization:
    org = db.query(Organization).filter_by(slug=slug).first()
    # Non-members get 404, not 403, so org slugs can't be probed.
    if not org or org_role(db, org.id, user) not in roles:
        raise HTTPException(404, "Organisation not found")
    return org


@router.post("", status_code=201)
def create_org(body: OrgIn, request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    require_organiser_mfa(user)
    org = Organization(name=body.name, slug=body.slug)
    db.add(org)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "That organisation URL is taken")
    db.add(OrgMember(org_id=org.id, user_id=user.id, role="owner"))
    audit(db, "org_created", user.id, detail=org.slug, request=request)
    db.commit()
    return {"id": org.id, "name": org.name, "slug": org.slug, "role": "owner"}


@router.get("")
def my_orgs(user: User = Depends(current_user), db: Session = Depends(get_db)):
    rows = (db.query(Organization, OrgMember.role)
              .join(OrgMember, OrgMember.org_id == Organization.id)
              .filter(OrgMember.user_id == user.id).all())
    return [{"id": o.id, "name": o.name, "slug": o.slug, "role": r} for o, r in rows]


@router.post("/{slug}/members", status_code=201)
def add_member(slug: str, body: MemberIn, request: Request, user: User = Depends(current_user),
               db: Session = Depends(get_db)):
    org = _org_for(db, slug, user, roles=("owner",))
    require_organiser_mfa(user)
    target = db.query(User).filter_by(email=body.email.lower()).first()
    if not target:
        raise HTTPException(404, "No account with that email. Ask them to register first.")
    if org_role(db, org.id, target):
        raise HTTPException(409, "Already a member")
    db.add(OrgMember(org_id=org.id, user_id=target.id, role=body.role))
    audit(db, "org_member_added", user.id, detail=f"{target.username} as {body.role} in {org.slug}", request=request)
    db.commit()
    return {"ok": True}


@router.post("/{slug}/events", status_code=201)
def create_event(slug: str, body: EventIn, request: Request, user: User = Depends(current_user),
                 db: Session = Depends(get_db)):
    org = _org_for(db, slug, user)
    require_organiser_mfa(user)
    if body.starts_at and body.ends_at and body.ends_at <= body.starts_at:
        raise HTTPException(422, "End time must be after start time")
    ev = Event(org_id=org.id, slug=body.slug, title=body.title, description=body.description,
               starts_at=body.starts_at, ends_at=body.ends_at,
               max_team_size=1 if body.team_mode == "solo" else body.max_team_size, team_mode=body.team_mode)
    db.add(ev)
    try:
        db.flush()
        audit(db, "event_created", user.id, ev.id, ev.slug, request)
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "That event URL is taken")
    return {"id": ev.id, "slug": ev.slug, "status": ev.status}


@router.get("/{slug}/events")
def org_events(slug: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    org = _org_for(db, slug, user)
    return [{"slug": e.slug, "title": e.title, "status": e.status}
            for e in db.query(Event).filter_by(org_id=org.id).order_by(Event.id)]
