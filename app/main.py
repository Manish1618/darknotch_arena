import logging
import re
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from redis.exceptions import RedisError
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import oidc, shared
from .config import settings
from .db import Base, engine
from .routers import admin, auth, events, manage, oauth, orgs, play

WEB_DIR = Path(__file__).resolve().parent.parent / "web"


@asynccontextmanager
async def lifespan(_app):
    if settings.cookie_secure:
        for name, val in (("FLAG_PEPPER", settings.flag_pepper), ("SECRET_KEY", settings.secret_key)):
            if len(val) < 32 or val.startswith("change-me"):
                raise RuntimeError(f"Set {name} (openssl rand -hex 32) before running in production")
        if not settings.smtp_host:
            raise RuntimeError("Set SMTP_HOST (and SMTP_USER, SMTP_PASSWORD, SMTP_FROM): "
                               "sign-up and password reset send codes by email")
        if settings.smtp_host == "log":
            logging.getLogger("mailer").warning(
                "SMTP_HOST=log: emails (with their sign-up codes) are written to this log, not sent. "
                "Players can't get their codes until real mail is set up.")
        if not settings.redis_url:
            raise RuntimeError("Set REDIS_URL: API containers share rate limits and the scoreboard through Redis")
    else:
        # Development: make the tables directly. Production runs the Alembic
        # migrations once (the "migrate" service) before any API container starts.
        Base.metadata.create_all(engine)
    yield


app = FastAPI(title="Darkn0tch Arena", docs_url=None, redoc_url=None, openapi_url=None,
              lifespan=lifespan)


UPLOAD_PATH = re.compile(r"^/api/events/[^/]+/manage/challenges/[0-9]+/files$")
UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}


class BodyLimit:
    """Refuses request bodies over the limit, whether or not the client sent a
    Content-Length (a chunked upload is counted as it arrives and cut off)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        limit = (settings.max_upload_bytes + 1024 * 1024 if UPLOAD_PATH.match(scope["path"])
                 else settings.max_body_bytes)
        length = dict(scope["headers"]).get(b"content-length")
        if length is not None and (not length.isdigit() or int(length) > limit):
            return await _too_large(send)
        seen, tripped, started = 0, False, False

        async def counted():
            nonlocal seen, tripped
            msg = await receive()
            if msg["type"] == "http.request":
                seen += len(msg.get("body", b""))
                if seen > limit:
                    tripped = True
                    raise _TooLarge()
            return msg

        async def guarded(msg):
            nonlocal started
            if msg["type"] == "http.response.start":
                started = True
                if tripped:  # whatever the app made of the cut-off body, the answer is 413
                    return await _too_large(send)
            if not tripped:
                await send(msg)

        try:
            await self.app(scope, counted, guarded)
        except _TooLarge:
            if not started:
                await _too_large(send)


class _TooLarge(Exception):
    pass


async def _too_large(send):
    body = b'{"detail":"Request is too large"}'
    await send({"type": "http.response.start", "status": 413,
                "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
    await send({"type": "http.response.body", "body": body})


@app.middleware("http")
async def security_headers(request: Request, call_next):
    # Browsers send Origin on cross-site requests. Refusing a foreign one on
    # every state change (login and register included) stops cross-site forms.
    origin = request.headers.get("origin")
    if (request.method in UNSAFE and origin
            and urlsplit(origin).netloc.lower() != request.headers.get("host", "").lower()):
        resp = JSONResponse({"detail": "Cross-site request refused"}, status_code=403)
    else:
        resp = await call_next(request)
    h = resp.headers
    h["X-Content-Type-Options"] = "nosniff"
    h["X-Frame-Options"] = "DENY"
    h["Referrer-Policy"] = "same-origin"
    # Scripts only from our own files (no inline JS). Inline *styles* are allowed
    # because the theme positions its animated pieces with style attributes.
    h["Content-Security-Policy"] = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
                                    "img-src 'self' data:; connect-src 'self'; object-src 'none'; "
                                    "frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
    h["Permissions-Policy"] = ("fullscreen=(self), camera=(), microphone=(), geolocation=(), payment=(), usb=()")
    h["Cross-Origin-Opener-Policy"] = "same-origin"
    h["Cross-Origin-Resource-Policy"] = "same-origin"
    if request.url.path.startswith("/api/") and "cache-control" not in h:
        h["Cache-Control"] = "no-store"  # nothing personal lingers in shared lab browsers
    if settings.cookie_secure:
        h["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return resp


app.add_middleware(BodyLimit)
if settings.allowed_hosts:
    # Requests for any other Host name get a 400 (stops Host-header tricks).
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_hosts + ["localhost", "127.0.0.1"])


app.include_router(auth.router)
app.include_router(orgs.router)
app.include_router(events.router)
app.include_router(play.router)
app.include_router(manage.router)
app.include_router(admin.router)
app.include_router(oauth.router)


@app.exception_handler(RedisError)
async def redis_down(_request: Request, _exc: RedisError):
    # Without shared rate limits it isn't safe to accept logins or flags: refuse, briefly.
    return JSONResponse({"detail": "The service is busy. Try again in a moment."}, status_code=503)


@app.get("/healthz")
def healthz():
    """Liveness: the process answers."""
    return {"ok": True}


@app.get("/readyz")
def readyz():
    """Readiness: the database and Redis answer too. Docker's health check and
    the load balancer use this, so a container that lost them gets no traffic."""
    try:
        with engine.connect() as c:
            c.execute(text("SELECT 1"))
        r = shared.redis_client()
        if r is not None:
            r.ping()
    except (SQLAlchemyError, RedisError):
        return JSONResponse({"ok": False}, status_code=503)
    return {"ok": True}


@app.get("/api/site")
def site():
    return {"name": settings.site_name, "organiser_mfa": settings.organiser_mfa,
            "providers": [{"id": k, "name": v["name"]} for k, v in oidc.providers().items()]}


app.mount("/static", StaticFiles(directory=WEB_DIR / "static"), name="static")

INDEX = WEB_DIR / "index.html"


@app.get("/{path:path}", include_in_schema=False)
def spa(path: str):
    """Every non-API page is the same shell; the browser app routes it."""
    if path.startswith(("api/", "static/")) or path == "api":
        raise HTTPException(404, "Not found")
    return FileResponse(INDEX, media_type="text/html", headers={"Cache-Control": "no-cache"})
