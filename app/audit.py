"""Writes to the audit log. Entries ride along in the caller's transaction."""
from fastapi import Request
from sqlalchemy.orm import Session

from .models import AuditLog, aware

LONG_FIELDS = {"description", "content"}  # named, not quoted, in the log


def client_ip(request: Request | None) -> str:
    # Behind Caddy, uvicorn --proxy-headers has already put the real client here.
    return (request.client.host if request and request.client else "")[:64]


def audit(db: Session, action: str, user_id: int | None = None, event_id: int | None = None,
          detail: str = "", request: Request | None = None) -> None:
    db.add(AuditLog(action=action[:40], user_id=user_id, event_id=event_id,
                    detail=detail[:300], ip=client_ip(request)))


def changes(obj, data: dict) -> str:
    """'field old -> new' for each value that actually changes. Call before applying."""
    out = []
    for k, new in sorted(data.items()):
        old = getattr(obj, k, None)
        o, n = (aware(old) if hasattr(old, "tzinfo") else old), (aware(new) if hasattr(new, "tzinfo") else new)
        if o == n:
            continue
        if k in LONG_FIELDS:
            out.append(k)
        else:
            fmt = lambda v: v.isoformat(timespec="minutes") if hasattr(v, "isoformat") else ("none" if v is None else str(v)[:40])
            out.append(f"{k} {fmt(o)} -> {fmt(n)}")
    return ", ".join(out)
