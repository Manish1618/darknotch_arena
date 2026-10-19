"""'Sign in with Google' (see app/oidc.py for the checks).

The state for each round trip is stored server-side (so any API container can
finish it), used once, expires in 10 minutes, and is tied to the browser that
started it by a cookie, which stops login-CSRF. If the account has 2FA on, the
provider sign-in is only the first factor: the person still types their code."""
import re
import secrets
from datetime import timedelta
from urllib.parse import quote

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from .. import oidc
from ..audit import audit, client_ip
from ..config import settings
from ..db import get_db
from ..mailer import quietly, security_mail
from ..models import AuthFlow, Identity, User, aware, utcnow
from ..schemas import TicketIn
from ..security import limiter, new_token, safe_eq, sha256
from ..site import email_problem, get_site
from .auth import _check_second_factor, signed_in

router = APIRouter(prefix="/api/auth/oauth", tags=["oauth"])
BROWSER = "__Host-oauth" if settings.cookie_secure else "oauth"
FLOW = timedelta(minutes=10)


def safe_next(path: str) -> str:
    """Only paths on this site, so the round trip can't bounce people elsewhere."""
    path = (path or "/")[:200]
    return path if path.startswith("/") and not path.startswith(("//", "/\\")) else "/"


def _to_login(message: str) -> RedirectResponse:
    r = RedirectResponse("/login?error=" + quote(message[:200]), status_code=303)
    r.delete_cookie(BROWSER, path="/", secure=settings.cookie_secure, httponly=True, samesite="lax")
    return r


def _sweep(db: Session) -> None:
    db.query(AuthFlow).filter(AuthFlow.expires < utcnow()).delete()


@router.get("/{provider}/start")
def start(provider: str, next: str = "/", db: Session = Depends(get_db)):
    if provider not in oidc.providers():
        raise HTTPException(404, "That sign-in method isn't set up on this site")
    state, nonce, browser = new_token(), new_token(), new_token()
    verifier, challenge = oidc.pkce_pair()
    _sweep(db)
    db.add(AuthFlow(token_hash=sha256(state), kind="oauth", provider=provider, expires=utcnow() + FLOW,
                    data={"nonce": nonce, "verifier": verifier, "next": safe_next(next), "browser": sha256(browser)}))
    db.commit()
    r = RedirectResponse(oidc.authorize_url(provider, state, nonce, challenge), status_code=303)
    r.set_cookie(BROWSER, browser, max_age=int(FLOW.total_seconds()), httponly=True,
                 secure=settings.cookie_secure, samesite="lax", path="/")
    return r


@router.get("/{provider}/callback")
def callback(provider: str, request: Request, background: BackgroundTasks, code: str = "", state: str = "",
             error: str = "", db: Session = Depends(get_db)):
    flow = db.query(AuthFlow).filter_by(token_hash=sha256(state), kind="oauth").first() if state else None
    if flow:  # single use, whatever happens next
        db.delete(flow)
        db.commit()
    if error:
        return _to_login("Sign-in was cancelled.")
    browser = request.cookies.get(BROWSER, "")
    if (not flow or flow.provider != provider or aware(flow.expires) < utcnow() or not code
            or not browser or not safe_eq(flow.data.get("browser", ""), sha256(browser))):
        return _to_login("That sign-in expired or was started in another browser. Try again.")
    try:
        who = oidc.fetch_identity(provider, code, flow.data["verifier"], flow.data["nonce"])
    except oidc.ProviderError as e:
        return _to_login(str(e))
    u, why = _account_for(db, request, background, provider, who)
    if why:
        return _to_login(why)
    name = oidc.providers()[provider]["name"]
    nxt = flow.data.get("next", "/")
    if u.totp_enabled:
        ticket = new_token()
        db.add(AuthFlow(token_hash=sha256(ticket), kind="ticket", provider=provider, user_id=u.id,
                        data={"next": nxt}, expires=utcnow() + timedelta(minutes=5)))
        db.commit()
        r = RedirectResponse(f"/login#ticket={ticket}", status_code=303)  # fragment: never sent to servers or logs
    else:
        r = RedirectResponse(nxt, status_code=303)
        signed_in(db, request, r, background, u, f"with {name}")
    r.delete_cookie(BROWSER, path="/", secure=settings.cookie_secure, httponly=True, samesite="lax")
    return r


@router.post("/finish")
def finish(body: TicketIn, request: Request, response: Response, background: BackgroundTasks,
           db: Session = Depends(get_db)):
    """Second step after a provider sign-in, for accounts with 2FA on."""
    ip, w = client_ip(request), 900
    if limiter.blocked(f"ticket:{ip}", 30, w):
        raise HTTPException(429, "Too many wrong codes. Try again in a few minutes.")
    flow = db.query(AuthFlow).filter_by(token_hash=sha256(body.ticket), kind="ticket").first()
    if not flow or aware(flow.expires) < utcnow() or flow.attempts >= 5:
        raise HTTPException(400, "That sign-in expired. Start again.")
    u = db.get(User, flow.user_id)
    if not u or not u.is_active:
        raise HTTPException(400, "That sign-in expired. Start again.")
    how = _check_second_factor(db, u, body.totp)
    if not how:
        flow.attempts += 1
        limiter.fail(f"ticket:{ip}", w)
        audit(db, "login_failed", u.id, detail="wrong 2FA code after provider sign-in", request=request)
        db.commit()
        raise HTTPException(401, "Wrong 2FA code")
    nxt = flow.data.get("next", "/")
    name = oidc.providers().get(flow.provider, {}).get("name", flow.provider)
    db.delete(flow)
    out = signed_in(db, request, response, background, u, f"with {name} and 2FA")
    return {**out, "next": nxt}


def _account_for(db: Session, request: Request, background: BackgroundTasks, provider: str, who: dict):
    """The account for this provider identity: already linked, linked now by
    its verified email, or newly created. Returns (user, None) or (None, why)."""
    ident = db.query(Identity).filter_by(provider=provider, subject=who["sub"]).first()
    name = oidc.providers()[provider]["name"]
    if ident:
        u = db.get(User, ident.user_id)
    else:
        u = db.query(User).filter_by(email=who["email"]).first()
        if u:
            db.add(Identity(user_id=u.id, provider=provider, subject=who["sub"], email=who["email"]))
            audit(db, "identity_linked", u.id, detail=name, request=request)
            when = utcnow().strftime("%d %b %Y, %H:%M UTC")
            background.add_task(quietly, security_mail, u.email, f"{name} sign-in linked", when, client_ip(request),
                                request.headers.get("user-agent", "")[:120])
        else:
            site = get_site(db)
            if not site["registration_open"]:
                return None, "Sign-up is closed on this site right now."
            why = email_problem(who["email"], site, check_dns=False)
            if why:
                return None, why
            # no password: they sign in with the provider (or set one with "Forgot password")
            u = User(email=who["email"], username=_free_username(db, who), pw_hash="!")
            db.add(u)
            db.flush()
            db.add(Identity(user_id=u.id, provider=provider, subject=who["sub"], email=who["email"]))
            audit(db, "register", u.id, detail=f"with {name}", request=request)
    if not u or not u.is_active:
        db.rollback()
        return None, "This account is disabled. Contact the site admin."
    db.commit()
    return u, None


def _free_username(db: Session, who: dict) -> str:
    base = re.sub(r"[^A-Za-z0-9_]", "_", who["email"].split("@")[0])[:24].strip("_") or "player"
    base = base if len(base) >= 3 else base + "_ctf"
    name = base
    while db.query(User.id).filter(User.username == name).first():
        name = f"{base}_{secrets.randbelow(10000):04d}"
    return name
