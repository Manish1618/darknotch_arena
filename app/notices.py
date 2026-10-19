"""Team pop-ups: what teammates did (solves, hints, wrong-flag penalties,
lockouts), organiser point changes, and announcements to the whole event."""
from sqlalchemy.orm import Session

from .models import Event, Notice, aware

# kinds the browser knows how to show; anything else is ignored there
KINDS = {"solve", "first_blood", "hint", "penalty", "lockout", "attempts",
         "award", "deduction", "instance_limit", "announcement",
         "disqualified", "reinstated", "member_removed"}


def notify(db: Session, ev: Event, kind: str, team_id: int | None, actor: str = "",
           subject: str = "", points: int = 0, text: str = "") -> Notice:
    """Queue a notice in the caller's transaction (it commits with the action)."""
    if kind not in KINDS:
        raise ValueError(f"unknown notice kind {kind!r}")
    n = Notice(event_id=ev.id, team_id=team_id, kind=kind, actor=actor[:32],
               subject=subject[:80], points=points, text=text[:500])
    db.add(n)
    return n


def view(n: Notice) -> dict:
    return {"id": n.id, "kind": n.kind, "team": n.team_id is not None, "actor": n.actor,
            "subject": n.subject, "points": n.points, "text": n.text,
            "at": aware(n.created).isoformat()}
