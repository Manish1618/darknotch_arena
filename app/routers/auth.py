import hmac
from datetime import datetime, timedelta, timezone

import pyotp
import segno
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, Response
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..audit import audit, client_ip
from ..config import settings
from ..db import get_db
from ..deps import COOKIE, current_session, current_user
from ..mailer import code_mail, quietly, security_mail
from ..models import AuditLog, AuthSession, EmailCode, RecoveryCode, User, aware, utcnow
from ..schemas import EmailIn, LoginIn, PasswordIn, RegisterIn, ResetIn, TotpIn, TotpOffIn
from ..security import (decrypt_secret, email_code_digest, encrypt_secret, hash_password, limiter,
                        needs_rehash, new_email_code, new_recovery_codes, new_token, new_totp_secret,
                        password_problem, recovery_digest, sha256, totp_step, verify_password)
from ..site import email_problem, get_site

router = APIRouter(prefix="/api/auth", tags=["auth"])

BAD_LOGIN = "Wrong email, password or 2FA code"


def _start_session(db: Session, request: Request, response: Response, user: User) -> dict:
    tok, csrf = new_token(), new_token()
    db.add(AuthSession(token_hash=sha256(tok), csrf=csrf, user_id=user.id, ip=client_ip(request),
                       user_agent=request.headers.get("user-agent", "")[:200],
                       expires=datetime.now(timezone.utc) + timedelta(seconds=settings.session_ttl_s)))
    db.commit()
    response.set_cookie(COOKIE, tok, max_age=settings.session_ttl_s, httponly=True,
                        secure=settings.cookie_secure, samesite="lax", path="/")
    return {"csrf": csrf, "user": _me(user)}


def signed_in(db: Session, request: Request, response: Response, background: BackgroundTasks, u: User,
              how: str = "") -> dict:
    """Finish any successful sign-in (password or Google): log it, open the session, and mail a 'was this you?' note the
    first time an IP is seen for this account in 90 days."""
    ip = client_ip(request)
    audit(db, "login", u.id, detail=how, request=request)
    out = _start_session(db, request, response, u)
    seen = (db.query(func.count(AuditLog.id))
              .filter(AuditLog.user_id == u.id, AuditLog.action == "login", AuditLog.ip == ip,
                      AuditLog.created >= utcnow() - timedelta(days=90)).scalar())
    if seen <= 1:
        when = utcnow().strftime("%d %b %Y, %H:%M UTC")
        ua = request.headers.get("user-agent", "")[:120]
        background.add_task(quietly, security_mail, u.email, "New sign-in" + (f" {how}" if how else ""), when, ip, ua)
    return out


def _me(u: User) -> dict:
    return {"id": u.id, "email": u.email, "username": u.username, "totp_enabled": u.totp_enabled,
            "is_admin": u.is_admin}


def _check_second_factor(db: Session, u: User, code: str) -> str | None:
    """'totp' or 'recovery' when the code is good, else None. A TOTP code works
    once; a recovery code is used up."""
    code = (code or "").strip()
    step = totp_step(decrypt_secret(u.totp_secret), code, after=u.totp_last_step or 0)
    if step is not None:
        u.totp_last_step = step
        return "totp"
    if len(code.replace("-", "")) == 10:
        rc = db.query(RecoveryCode).filter_by(user_id=u.id, digest=recovery_digest(code), used=None).first()
        if rc:
            rc.used = utcnow()
            return "recovery"
    return None


def _issue_recovery_codes(db: Session, u: User) -> list[str]:
    db.query(RecoveryCode).filter_by(user_id=u.id).delete()
    codes = new_recovery_codes()
    db.add_all(RecoveryCode(user_id=u.id, digest=recovery_digest(c)) for c in codes)
    return codes


# --- emailed codes: proof for a password reset ---------------------------------

CODE_SENT = "If that address can receive mail, a 6-digit code is on its way. It works for 10 minutes."
BAD_CODE = "That code is wrong or has expired. Ask for a new one."
CODE_TRIES = 5  # wrong guesses before a code stops working


def _send_limit(email: str, ip: str) -> None:
    """One code a minute and five an hour per address; a cap per IP so nobody
    can use the site to flood other people's inboxes."""
    keys = [(f"mail1:{email}", 1, 60), (f"mailh:{email}", 5, 3600), (f"mailip:{ip}", settings.mail_per_ip_hour, 3600)]
    if any(limiter.blocked(k, n, w) for k, n, w in keys):
        raise HTTPException(429, "Wait a minute before asking for another code.")
    for k, _n, w in keys:
        limiter.fail(k, w)


def _issue_code(db: Session, email: str, purpose: str) -> str:
    code = new_email_code()
    row = db.query(EmailCode).filter_by(email=email, purpose=purpose).first() or EmailCode(email=email, purpose=purpose)
    row.digest, row.attempts = email_code_digest(email, purpose, code), 0
    row.created, row.expires = utcnow(), utcnow() + timedelta(seconds=settings.email_code_ttl_s)
    db.add(row)
    return code


def _take_code(db: Session, request: Request, email: str, purpose: str, code: str) -> None:
    """Spend a mailed code, or raise. Wrong guesses count against the code and the IP."""
    ip, w = client_ip(request), 900
    if limiter.blocked(f"codeip:{ip}", 50, w):
        raise HTTPException(429, "Too many wrong codes. Try again in a few minutes.")
    row = db.query(EmailCode).filter_by(email=email, purpose=purpose).first()
    good = (row is not None and aware(row.expires) > utcnow() and row.attempts < CODE_TRIES
            and hmac.compare_digest(row.digest, email_code_digest(email, purpose, code)))
    if not good:
        limiter.fail(f"codeip:{ip}", w)
        if row is not None:
            row.attempts += 1
            db.commit()
        raise HTTPException(400, BAD_CODE)
    db.delete(row)


SIGNUPS_PER_IP_HOUR = 100  # a whole college lab can share one IP


@router.post("/register", status_code=201)
def register(body: RegisterIn, request: Request, response: Response, db: Session = Depends(get_db)):
    """Sign up with an email, a username and a password, in one step. The
    address must be on a domain that receives mail (no throwaway inboxes) and
    fit the site's sign-up rules."""
    site = get_site(db)
    if not site["registration_open"]:
        raise HTTPException(403, "Sign-up is closed on this site right now.")
    email = body.email.lower()
    why = email_problem(email, site)
    if why:
        raise HTTPException(422, why)
    problem = password_problem(body.password, body.username, email)
    if problem:
        raise HTTPException(422, problem)
    ip = client_ip(request)
    if limiter.blocked(f"signup:{ip}", SIGNUPS_PER_IP_HOUR, 3600):
        raise HTTPException(429, "Too many new accounts from this network. Try again later.")
    limiter.fail(f"signup:{ip}", 3600)
    u = User(email=email, username=body.username, pw_hash=hash_password(body.password))
    db.add(u)
    try:
        db.flush()
        audit(db, "register", u.id, request=request)
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "That username is taken, or this email already has an account")
    return _start_session(db, request, response, u)


@router.post("/password/forgot")
def password_forgot(body: EmailIn, request: Request, background: BackgroundTasks, db: Session = Depends(get_db)):
    """Mail a reset code. Same answer, and the same speed, whether or not the
    account exists: the email goes out after the response."""
    email = body.email.lower()
    _send_limit(email, client_ip(request))
    u = db.query(User).filter_by(email=email).first()
    if u and u.is_active:
        code = _issue_code(db, email, "reset")
        db.commit()
        background.add_task(quietly, code_mail, email, "reset", code)
    return {"ok": True, "message": CODE_SENT}


@router.post("/password/reset")
def password_reset(body: ResetIn, request: Request, db: Session = Depends(get_db)):
    """Set a new password with a mailed code. Every session ends; 2FA stays on,
    so a stolen inbox alone still can't log in to a 2FA account."""
    email = body.email.lower()
    _take_code(db, request, email, "reset", body.code)
    u = db.query(User).filter_by(email=email).first()
    if not u or not u.is_active:
        db.rollback()
        raise HTTPException(400, BAD_CODE)
    problem = password_problem(body.password, u.username, u.email)
    if problem:
        db.rollback()  # keep the code so they can pick a better password
        raise HTTPException(422, problem)
    u.pw_hash = hash_password(body.password)
    db.query(AuthSession).filter(AuthSession.user_id == u.id).delete()
    limiter.clear(f"acct:{email}")
    audit(db, "password_reset", u.id, request=request)
    db.commit()
    return {"ok": True}


@router.post("/login")
def login(body: LoginIn, request: Request, response: Response, background: BackgroundTasks,
          db: Session = Depends(get_db)):
    email, ip = body.email.lower(), client_ip(request)
    w = settings.login_window_s
    if (limiter.blocked(f"acct:{email}", settings.login_fail_account, w)
            or limiter.blocked(f"ip:{ip}", settings.login_fail_ip, w)):
        raise HTTPException(429, "Too many failed logins. Try again in a few minutes.")
    u = db.query(User).filter_by(email=email).first()
    ok = verify_password(body.password, u.pw_hash if u else None) and u is not None and u.is_active
    how = "password"
    if ok and u.totp_enabled:
        how = _check_second_factor(db, u, body.totp or "")
        ok = how is not None
    if not ok:
        limiter.fail(f"acct:{email}", w)
        limiter.fail(f"ip:{ip}", w)
        if u:
            audit(db, "login_failed", u.id, request=request)
            db.commit()
        raise HTTPException(401, BAD_LOGIN)
    limiter.clear(f"acct:{email}")
    if needs_rehash(u.pw_hash):
        u.pw_hash = hash_password(body.password)
    out = signed_in(db, request, response, background, u, "with a recovery code" if how == "recovery" else "")
    if how == "recovery":
        out["recovery_codes_left"] = db.query(RecoveryCode).filter_by(user_id=u.id, used=None).count()
    return out


@router.post("/logout")
def logout(response: Response, s: AuthSession = Depends(current_session), db: Session = Depends(get_db)):
    db.delete(s)
    db.commit()
    response.delete_cookie(COOKIE, path="/", secure=settings.cookie_secure, httponly=True, samesite="lax")
    return {"ok": True}


@router.get("/me")
def me(s: AuthSession = Depends(current_session), u: User = Depends(current_user), db: Session = Depends(get_db)):
    out = {"user": _me(u), "csrf": s.csrf}
    if u.totp_enabled:
        out["recovery_codes_left"] = db.query(RecoveryCode).filter_by(user_id=u.id, used=None).count()
    return out


# --- two-factor login --------------------------------------------------------

@router.post("/2fa/setup")
def totp_setup(u: User = Depends(current_user), db: Session = Depends(get_db)):
    if u.totp_enabled:
        raise HTTPException(400, "2FA is already on")
    secret = new_totp_secret()
    u.totp_secret = encrypt_secret(secret)
    db.commit()
    uri = pyotp.TOTP(secret).provisioning_uri(name=u.email, issuer_name=settings.site_name)
    # drawn here so the page needs no QR script and the secret never leaves this response
    qr = segno.make(uri, error="m").svg_data_uri(scale=5, border=2, dark="#000", light="#fff")
    return {"secret": secret, "otpauth_uri": uri, "qr": qr}


@router.post("/2fa/enable")
def totp_enable(body: TotpIn, request: Request, response: Response,
                u: User = Depends(current_user), db: Session = Depends(get_db)):
    step = totp_step(decrypt_secret(u.totp_secret), body.code) if u.totp_secret else None
    if step is None:
        raise HTTPException(400, "Code didn't match. Check your authenticator's clock.")
    u.totp_enabled, u.totp_last_step = True, step
    codes = _issue_recovery_codes(db, u)
    # Sessions opened without 2FA end; this browser gets a fresh one.
    db.query(AuthSession).filter(AuthSession.user_id == u.id).delete()
    audit(db, "2fa_on", u.id, request=request)
    out = _start_session(db, request, response, u)
    return {**out, "recovery_codes": codes}


@router.post("/2fa/disable")
def totp_disable(body: TotpOffIn, request: Request, s: AuthSession = Depends(current_session),
                 u: User = Depends(current_user), db: Session = Depends(get_db)):
    if not u.totp_enabled:
        raise HTTPException(400, "2FA is already off")
    if not verify_password(body.password, u.pw_hash) or not _check_second_factor(db, u, body.code):
        db.commit()  # keep a spent code spent
        raise HTTPException(400, "Wrong password or code")
    u.totp_enabled, u.totp_secret, u.totp_last_step = False, None, 0
    db.query(RecoveryCode).filter_by(user_id=u.id).delete()
    db.query(AuthSession).filter(AuthSession.user_id == u.id, AuthSession.id != s.id).delete()
    audit(db, "2fa_off", u.id, request=request)
    db.commit()
    return {"ok": True, "user": _me(u)}


@router.post("/2fa/recovery-codes")
def new_codes(body: TotpIn, request: Request, u: User = Depends(current_user), db: Session = Depends(get_db)):
    """Replace the recovery codes; needs a current code from the app."""
    if not u.totp_enabled or _check_second_factor(db, u, body.code) != "totp":
        db.commit()
        raise HTTPException(400, "Enter a current code from your authenticator app")
    codes = _issue_recovery_codes(db, u)
    audit(db, "recovery_codes_new", u.id, request=request)
    db.commit()
    return {"recovery_codes": codes}


# --- password and sessions ---------------------------------------------------

@router.post("/password")
def change_password(body: PasswordIn, request: Request, s: AuthSession = Depends(current_session),
                    u: User = Depends(current_user), db: Session = Depends(get_db)):
    if not verify_password(body.current, u.pw_hash):
        audit(db, "password_change_failed", u.id, request=request)
        db.commit()
        raise HTTPException(400, "Your current password is wrong")
    problem = password_problem(body.new, u.username, u.email)
    if problem:
        raise HTTPException(422, problem)
    u.pw_hash = hash_password(body.new)
    # anyone holding an old session is signed out; this browser stays in
    db.query(AuthSession).filter(AuthSession.user_id == u.id, AuthSession.id != s.id).delete()
    audit(db, "password_changed", u.id, request=request)
    db.commit()
    return {"ok": True}


@router.get("/sessions")
def sessions(s: AuthSession = Depends(current_session), db: Session = Depends(get_db)):
    rows = db.query(AuthSession).filter_by(user_id=s.user_id).order_by(AuthSession.last_seen.desc())
    return [{"id": r.id, "current": r.id == s.id, "ip": r.ip, "user_agent": r.user_agent,
             "created": aware(r.created).isoformat(), "last_seen": aware(r.last_seen).isoformat()} for r in rows]


@router.post("/sessions/revoke-others")
def revoke_others(request: Request, s: AuthSession = Depends(current_session), db: Session = Depends(get_db)):
    n = db.query(AuthSession).filter(AuthSession.user_id == s.user_id, AuthSession.id != s.id).delete()
    audit(db, "sessions_revoked", s.user_id, detail=f"{n} other sessions", request=request)
    db.commit()
    return {"ok": True, "ended": n}


@router.delete("/sessions/{sid}")
def revoke(sid: int, request: Request, s: AuthSession = Depends(current_session), db: Session = Depends(get_db)):
    r = db.get(AuthSession, sid)
    if not r or r.user_id != s.user_id:
        raise HTTPException(404, "Session not found")
    db.delete(r)
    audit(db, "session_revoked", s.user_id, request=request)
    db.commit()
    return {"ok": True}


@router.get("/activity")
def activity(u: User = Depends(current_user), db: Session = Depends(get_db)):
    """Your own recent security events, so you can spot a stranger."""
    rows = (db.query(AuditLog).filter(AuditLog.user_id == u.id, AuditLog.event_id.is_(None))
              .order_by(AuditLog.id.desc()).limit(30))
    return [{"action": a.action, "detail": a.detail, "ip": a.ip, "at": aware(a.created).isoformat()} for a in rows]
