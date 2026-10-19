"""Short-lived cache for the public scoreboard.

Every viewer polls the board every few seconds, so with hundreds of viewers the
same standings would be rebuilt hundreds of times a second. An answer is kept
for a few seconds per event and shared by everyone watching. It is dropped as
soon as anything that changes scores is committed (a solve, a paid hint, a
wrong-flag penalty, an organiser's point change, a challenge or event edit), so
a capture still shows up on the next poll. Wrong flags without a penalty only
move the activity strip, which may lag by up to BOARD_CACHE_S.

With REDIS_URL set the cache lives in Redis and is shared by every API
container; without it, it lives in this process (development and tests)."""
import json
import threading
import time

from sqlalchemy import event as sa_event
from sqlalchemy.orm import Session

from . import shared
from .config import settings
from .models import Award, Challenge, Event, Hint, HintUnlock, Solve, Submission, Team

_lock = threading.Lock()
_store: dict[tuple, tuple[float, object]] = {}   # (event id, key) -> (expires, value)
_gates: dict[tuple, threading.Lock] = {}         # one builder per key; the rest wait for it
_gen: dict[int, int] = {}                        # event id -> bumped on every invalidation
_ALL = -1                                        # "some event changed, we can't tell which"


def get(event_id: int, key: tuple, build):
    """The cached value for this event and key, building it once if missing.
    Callers must not modify what they get back: other viewers share it."""
    if settings.board_cache_s <= 0:
        return build()
    r = shared.redis_client()
    if r is not None:
        return _get_shared(r, event_id, key, build)
    k = (event_id, key)
    with _lock:
        hit = _store.get(k)
        if hit and hit[0] > time.monotonic():
            return hit[1]
        gate = _gates.setdefault(k, threading.Lock())
    with gate:
        with _lock:
            hit = _store.get(k)
            if hit and hit[0] > time.monotonic():
                return hit[1]
            gen = (_gen.get(event_id, 0), _gen.get(_ALL, 0))
        value = build()
        with _lock:
            # keep it only if nothing was committed for this event while we built it
            if settings.board_cache_s > 0 and gen == (_gen.get(event_id, 0), _gen.get(_ALL, 0)):
                _store[k] = (time.monotonic() + settings.board_cache_s, value)
        return value


def _get_shared(r, event_id: int, key: tuple, build):
    """Redis version: the generation counters are part of the key, so bumping
    one makes every container miss at once; old entries just expire."""
    gen, every = r.mget(f"bc:gen:{event_id}", "bc:gen:all")
    k = f"bc:{event_id}:{gen or 0}:{every or 0}:{json.dumps(key)}"
    hit = r.get(k)
    if hit is not None:
        return json.loads(hit)
    # one container builds; the others wait briefly for its answer
    if not r.set(k + ":lock", "1", nx=True, ex=5):
        for _ in range(20):
            time.sleep(0.05)
            hit = r.get(k)
            if hit is not None:
                return json.loads(hit)
    value = build()
    r.set(k, json.dumps(value), ex=max(1, int(settings.board_cache_s)))
    r.delete(k + ":lock")
    return value


def invalidate(event_ids) -> None:
    r = shared.redis_client()
    if r is not None:
        p = r.pipeline()
        for eid in event_ids:
            p.incr("bc:gen:all" if eid == _ALL else f"bc:gen:{eid}")
        p.execute()
    with _lock:
        for eid in event_ids:
            _gen[eid] = _gen.get(eid, 0) + 1
        if _ALL in event_ids:
            _store.clear()
        else:
            for k in [k for k in _store if k[0] in event_ids]:
                del _store[k]


def clear() -> None:
    with _lock:
        _store.clear()
        _gates.clear()
        _gen.clear()


def _touched(obj) -> int | None:
    """The event whose board this row can change, _ALL if unknown, else None."""
    if isinstance(obj, Event):
        return obj.id
    if isinstance(obj, (Solve, HintUnlock, Award, Challenge, Team)):
        return obj.event_id
    if isinstance(obj, Submission):
        return obj.event_id if obj.penalty else None
    if isinstance(obj, Hint):
        return _ALL  # a hint's cost moves scores; it only knows its challenge
    return None


@sa_event.listens_for(Session, "after_flush")
def _collect(session, _ctx):
    dirty = session.info.setdefault("board_dirty", set())
    for obj in (*session.new, *session.dirty, *session.deleted):
        eid = _touched(obj)
        if eid is not None:
            dirty.add(eid)


@sa_event.listens_for(Session, "after_commit")
def _flush_cache(session):
    dirty = session.info.pop("board_dirty", None)
    if dirty:
        invalidate(dirty)


@sa_event.listens_for(Session, "after_rollback")
def _forget(session):
    session.info.pop("board_dirty", None)
