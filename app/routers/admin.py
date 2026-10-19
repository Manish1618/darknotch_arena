"""Site admin: accounts, sign-up rules, the site-wide security log, and event
moderation. Only users made admin from the server command line get in
(python -m app.cli make-admin); everyone else gets 404. Admins need 2FA on
whenever organisers do, and every change is written to the audit log."""
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from ..audit import audit
from ..db import get_db
from ..deps import current_user, require_organiser_mfa
from ..models import (AuditLog, AuthSession, Event, Organization, OrgMember, RecoveryCode, Team,
                      User, aware, utcnow)
from ..schemas import EventStatusIn, SiteIn, StepUpIn
from ..security import decrypt_secret, totp_step
from ..site import get_site, set_site

router = APIRouter(prefix="/api/admin", tags=["admin"])
PAGE = 50


def site_admin(user: User = Depends(current_user)) -> User:
    if not user.is_admin:
        raise HTTPException(404, "Not found")
    require_organiser_mfa(user)
    return user


def _target(db: Session, uid: int, admin: User) -> User:
    u = db.get(User, uid)
    if not u:
        raise HTTPException(404, "No such user")
    if u.id == admin.id or u.is_admin:
        raise HTTPException(403, "Admins are changed from the server command line, not here")
    return u


def _step_up(db: Session, admin: User, code: str) -> None:
    """Risky actions need a fresh code from the admin's own authenticator."""
    if not admin.totp_enabled:
        return
    step = totp_step(decrypt_secret(admin.totp_secret), code, after=admin.totp_last_step or 0)
    if step is None:
        raise HTTPException(400, "Enter a current code from your authenticator app")
    admin.totp_last_step = step


@router.get("/overview")
def overview(admin: User = Depends(site_admin), db: Session = Depends(get_db)):
    now = utcnow()
    day, recent = now - timedelta(hours=24), now - timedelta(minutes=15)
    count = lambda q: q.scalar() or 0  # noqa: E731
    acts = dict(db.query(AuditLog.action, func.count(AuditLog.id)).filter(AuditLog.created >= day)
                  .group_by(AuditLog.action).all())
    events = dict(db.query(Event.status, func.count(Event.id)).group_by(Event.status).all())
    return {
        "users": count(db.query(func.count(User.id))),
        "disabled": count(db.query(func.count(User.id)).filter(User.is_active.is_(False))),
        "with_2fa": count(db.query(func.count(User.id)).filter(User.totp_enabled.is_(True))),
        "admins": count(db.query(func.count(User.id)).filter(User.is_admin.is_(True))),
        "signups_24h": count(db.query(func.count(User.id)).filter(User.created >= day)),
        "logins_24h": acts.get("login", 0), "failed_logins_24h": acts.get("login_failed", 0),
        "resets_24h": acts.get("password_reset", 0),
        "online": count(db.query(func.count(func.distinct(AuthSession.user_id))).filter(AuthSession.last_seen >= recent)),
        "orgs": count(db.query(func.count(Organization.id))),
        "events": events, "site": get_site(db),
    }


@router.get("/users")
def users(q: str = Query("", max_length=80), page: int = Query(0, ge=0, le=10000), only: str = Query("", pattern=r"^(|disabled|admins|no2fa)$"),
          admin: User = Depends(site_admin), db: Session = Depends(get_db)):
    last = (db.query(AuthSession.user_id, func.max(AuthSession.last_seen).label("seen"))
              .group_by(AuthSession.user_id).subquery())
    query = db.query(User, last.c.seen).outerjoin(last, last.c.user_id == User.id)
    if q.strip():
        like = f"%{q.strip().lower()}%"
        query = query.filter(or_(func.lower(User.username).like(like), User.email.like(like)))
    if only == "disabled":
        query = query.filter(User.is_active.is_(False))
    elif only == "admins":
        query = query.filter(User.is_admin.is_(True))
    elif only == "no2fa":
        query = query.filter(User.totp_enabled.is_(False))
    total = query.count()
    rows = query.order_by(User.id.desc()).offset(page * PAGE).limit(PAGE).all()
    orgs = dict(db.query(OrgMember.user_id, func.count(OrgMember.id))
                  .filter(OrgMember.user_id.in_([u.id for u, _ in rows] or [0])).group_by(OrgMember.user_id).all())
    return {"total": total, "page": page, "per_page": PAGE, "users": [{
        "id": u.id, "username": u.username, "email": u.email, "created": aware(u.created).isoformat(),
        "active": u.is_active, "admin": u.is_admin, "totp": u.totp_enabled, "organiser_of": orgs.get(u.id, 0),
        "last_seen": aware(seen).isoformat() if seen else None} for u, seen in rows]}


@router.post("/users/{uid}/disable")
def disable(uid: int, request: Request, admin: User = Depends(site_admin), db: Session = Depends(get_db)):
    u = _target(db, uid, admin)
    u.is_active = False
    n = db.query(AuthSession).filter(AuthSession.user_id == u.id).delete()
    audit(db, "admin_user_disabled", admin.id, detail=f"{u.username} ({n} sessions ended)", request=request)
    db.commit()
    return {"ok": True}


@router.post("/users/{uid}/enable")
def enable(uid: int, request: Request, admin: User = Depends(site_admin), db: Session = Depends(get_db)):
    u = _target(db, uid, admin)
    u.is_active = True
    audit(db, "admin_user_enabled", admin.id, detail=u.username, request=request)
    db.commit()
    return {"ok": True}


@router.post("/users/{uid}/logout")
def logout_everywhere(uid: int, request: Request, admin: User = Depends(site_admin), db: Session = Depends(get_db)):
    u = _target(db, uid, admin)
    n = db.query(AuthSession).filter(AuthSession.user_id == u.id).delete()
    audit(db, "admin_sessions_ended", admin.id, detail=f"{u.username}: {n}", request=request)
    db.commit()
    return {"ok": True, "ended": n}


@router.post("/users/{uid}/reset-2fa")
def reset_2fa(uid: int, body: StepUpIn, request: Request, admin: User = Depends(site_admin), db: Session = Depends(get_db)):
    """For someone who lost both their phone and their recovery codes. Check
    who they are first; their sessions end and they set 2FA up again."""
    u = _target(db, uid, admin)
    _step_up(db, admin, body.code)
    if not u.totp_enabled:
        raise HTTPException(400, "That account doesn't have 2FA on")
    u.totp_enabled, u.totp_secret, u.totp_last_step = False, None, 0
    db.query(RecoveryCode).filter_by(user_id=u.id).delete()
    db.query(AuthSession).filter(AuthSession.user_id == u.id).delete()
    audit(db, "admin_2fa_reset", admin.id, detail=u.username, request=request)
    audit(db, "2fa_off", u.id, detail="reset by a site admin", request=request)
    db.commit()
    return {"ok": True}


@router.get("/settings")
def get_settings(admin: User = Depends(site_admin), db: Session = Depends(get_db)):
    return get_site(db)


@router.put("/settings")
def put_settings(body: SiteIn, request: Request, admin: User = Depends(site_admin), db: Session = Depends(get_db)):
    before, data = get_site(db), body.model_dump()
    what = ", ".join(f"{k} {before[k]} -> {v}" for k, v in data.items() if before.get(k) != v)
    set_site(db, data)
    if what:
        audit(db, "site_settings", admin.id, detail=what[:300], request=request)
    db.commit()
    return get_site(db)


@router.get("/audit")
def site_audit(action: str = Query("", max_length=40), limit: int = Query(200, ge=1, le=500),
               admin: User = Depends(site_admin), db: Session = Depends(get_db)):
    q = (db.query(AuditLog, User.username, Event.slug).outerjoin(User, User.id == AuditLog.user_id)
           .outerjoin(Event, Event.id == AuditLog.event_id))
    if action:
        q = q.filter(AuditLog.action == action)
    return [{"at": aware(a.created).isoformat(), "user": u or "?", "event": e, "action": a.action,
             "detail": a.detail, "ip": a.ip} for a, u, e in q.order_by(AuditLog.id.desc()).limit(limit)]


@router.get("/events")
def all_events(admin: User = Depends(site_admin), db: Session = Depends(get_db)):
    teams = dict(db.query(Team.event_id, func.count(Team.id)).group_by(Team.event_id).all())
    rows = db.query(Event, Organization.name).join(Organization, Organization.id == Event.org_id).order_by(Event.id.desc())
    return [{"slug": e.slug, "title": e.title, "org": o, "status": e.status, "teams": teams.get(e.id, 0),
             "starts_at": aware(e.starts_at).isoformat() if e.starts_at else None,
             "ends_at": aware(e.ends_at).isoformat() if e.ends_at else None} for e, o in rows]


@router.post("/events/{slug}/status")
def event_status(slug: str, body: EventStatusIn, request: Request, admin: User = Depends(site_admin),
                 db: Session = Depends(get_db)):
    """Take an event down (or bring it back) without being one of its organisers."""
    ev = db.query(Event).filter_by(slug=slug).first()
    if not ev:
        raise HTTPException(404, "Event not found")
    old, ev.status = ev.status, body.status
    audit(db, "admin_event_status", admin.id, ev.id, f"status {old} -> {body.status}", request)
    db.commit()
    return {"ok": True, "status": ev.status}
