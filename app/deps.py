from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from .config import settings
from .db import get_db
from .models import AuthSession, Event, OrgMember, User, aware
from .security import safe_eq, sha256

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
# __Host- cookies must be Secure, path=/ and carry no Domain, so no subdomain
# or plain-http page can set or overwrite them.
COOKIE = "__Host-session" if settings.cookie_secure else "session"


def _load_session(request: Request, db: Session) -> AuthSession | None:
    tok = request.cookies.get(COOKIE)
    if not tok:
        return None
    s = db.query(AuthSession).filter_by(token_hash=sha256(tok)).first()
    if not s:
        return None
    now = datetime.now(timezone.utc)
    if aware(s.expires) < now or aware(s.last_seen) + timedelta(seconds=settings.session_idle_s) < now:
        db.delete(s)
        db.commit()
        return None
    if aware(s.last_seen) < now - timedelta(seconds=60):  # one write a minute is plenty
        s.last_seen = now
        db.commit()
    return s


def _same_origin(request: Request) -> bool:
    """Browsers send Origin on cross-site POSTs; refuse one from another site."""
    origin = request.headers.get("origin")
    if not origin:
        return True
    return urlsplit(origin).netloc.lower() == request.headers.get("host", "").lower()


def current_session(request: Request, db: Session = Depends(get_db)) -> AuthSession:
    s = _load_session(request, db)
    if not s:
        raise HTTPException(401, "Not logged in")
    # CSRF: every state-changing request must echo the per-session token,
    # and must not come from another site.
    if request.method not in SAFE_METHODS:
        sent = request.headers.get("x-csrf-token", "")
        if not sent or not safe_eq(sent, s.csrf) or not _same_origin(request):
            raise HTTPException(403, "Missing or invalid CSRF token")
    return s


def current_user(s: AuthSession = Depends(current_session), db: Session = Depends(get_db)) -> User:
    u = db.get(User, s.user_id)
    if not u or not u.is_active:
        raise HTTPException(401, "Not logged in")
    return u


def optional_user(request: Request, db: Session = Depends(get_db)) -> User | None:
    s = _load_session(request, db)
    return db.get(User, s.user_id) if s else None


def org_role(db: Session, org_id: int, user: User | None) -> str | None:
    if not user:
        return None
    m = db.query(OrgMember).filter_by(org_id=org_id, user_id=user.id).first()
    return m.role if m else None


def get_visible_event(db: Session, slug: str, user: User | None) -> Event:
    """Published events are public. Drafts exist only for the organising team;
    everyone else gets the same 404 as a missing event."""
    ev = db.query(Event).filter_by(slug=slug).first()
    if not ev:
        raise HTTPException(404, "Event not found")
    if ev.status == "published":
        return ev
    if org_role(db, ev.org_id, user):
        return ev
    raise HTTPException(404, "Event not found")


def require_organiser_mfa(user: User) -> None:
    """With ORGANISER_MFA=required, organiser tools need 2FA on the account."""
    if settings.organiser_mfa == "required" and not user.totp_enabled:
        raise HTTPException(403, "Turn on two-factor login (Account page) before managing events")
