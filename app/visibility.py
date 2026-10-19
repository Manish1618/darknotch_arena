"""Which parts of an event players and the public may see. Organisers choose
per event on the Player view page; every API route that serves one of these
parts checks it here, so hiding something in the page can't be bypassed by
calling the API directly. Organisers always see everything."""
from fastapi import HTTPException
from sqlalchemy.orm import Session

from .deps import org_role
from .models import Event, TeamMember, User

LEVELS = ("public", "players", "organisers")  # who may see: anyone, team members, organisers only
PARTS = {
    "scoreboard": "public",        # standings, top score, the live stage
    "graph": "public",             # race chart (score over time)
    "feed": "public",              # recent captures and the activity strip
    "challenge_stats": "public",   # solve counts and who drew first blood
    "solvers": "players",          # list of teams that solved a challenge
    "teams": "public",             # list of registered teams
    "matrix": "organisers",        # which team solved which challenge
}
_RANK = {lvl: i for i, lvl in enumerate(LEVELS)}


def rules(ev: Event) -> dict:
    saved = ev.visibility or {}
    return {k: saved.get(k) if saved.get(k) in _RANK else d for k, d in PARTS.items()}


def viewer_level(db: Session, ev: Event, user: User | None) -> str:
    if not user:
        return "public"
    if org_role(db, ev.org_id, user):
        return "organisers"
    if db.query(TeamMember.id).filter_by(event_id=ev.id, user_id=user.id).first():
        return "players"
    return "public"


def can_see(db: Session, ev: Event, user: User | None) -> dict:
    lvl = _RANK[viewer_level(db, ev, user)]
    return {k: lvl >= _RANK[need] for k, need in rules(ev).items()}


def require(db: Session, ev: Event, user: User | None, part: str) -> None:
    if not can_see(db, ev, user)[part]:
        raise HTTPException(403, "The organisers have hidden this part of the event")
