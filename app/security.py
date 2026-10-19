import base64
import hashlib
import hmac
import secrets
import threading
import time

import pyotp
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from . import shared
from .config import settings

_ph = PasswordHasher()  # argon2id with library defaults
# Verified against when the account doesn't exist, so timing doesn't reveal it.
_DUMMY_HASH = _ph.hash("not-a-real-password")


def hash_password(pw: str) -> str:
    return _ph.hash(pw)


def verify_password(pw: str, pw_hash: str | None) -> bool:
    try:
        return _ph.verify(pw_hash or _DUMMY_HASH, pw) and pw_hash is not None
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(pw_hash: str) -> bool:
    return _ph.check_needs_rehash(pw_hash)


# Long passwords people still pick; anything shorter than 10 is refused anyway.
COMMON_PASSWORDS = set("""
1234567890 0123456789 0987654321 1111111111 0000000000 1234512345 1122334455 123456789a
a123456789 12345678910 qwertyuiop qwertyuiop1 1q2w3e4r5t 1qaz2wsx3edc zaq12wsxcde qwerty1234
qwerty12345 asdfghjkl1 zxcvbnm123 password12 password123 password1! password1234 passw0rd123
p@ssw0rd123 p@ssword123 iloveyou12 iloveyou123 letmein123 welcome123 welcome1234 admin12345
admin123456 administrator changeme123 abcdefghij abcd123456 abc1234567 football123 baseball123
monkey1234 dragon1234 sunshine123 princess123 superman123 trustno1234 starwars123 computer123
internet123 secret1234 hello12345 freedom123 whatever123 master1234 michael123 jordan2323
charlie123 shadow1234 hackerman hacker12345 security123 cybersecurity capturetheflag ctfplayer1
ctf1234567 flag123456 rootroot12 toortoor12 kalilinux1 linux12345 ubuntu1234
""".split())


def password_problem(pw: str, username: str = "", email: str = "") -> str | None:
    """A reason to refuse this password, or None. Length is checked by the schema."""
    low = pw.lower()
    if low in COMMON_PASSWORDS or low.rstrip("!.@#") in COMMON_PASSWORDS:
        return "That password is on the list of the most common ones. Pick something less guessable."
    if len(set(low)) < 4:
        return "Use more than three different characters."
    for part in (username, email.split("@")[0]):
        if len(part) >= 3 and part.lower() in low:
            return "Don't put your username or email in your password."
    return None


# --- secrets at rest ---------------------------------------------------------
_DEV_KEY = "dev-only-secret-key"


def _key(purpose: str) -> bytes:
    """A separate 32-byte key per purpose, derived from SECRET_KEY."""
    return hmac.new((settings.secret_key or _DEV_KEY).encode(), purpose.encode(), hashlib.sha256).digest()


def encrypt_secret(plain: str) -> str:
    nonce = secrets.token_bytes(12)
    ct = AESGCM(_key("totp-secret")).encrypt(nonce, plain.encode(), b"totp")
    return "v1:" + base64.urlsafe_b64encode(nonce + ct).decode()


def decrypt_secret(stored: str | None) -> str:
    if not stored:
        return ""
    if not stored.startswith("v1:"):
        return stored  # written before encryption existed (dev databases)
    try:
        raw = base64.urlsafe_b64decode(stored[3:])
        return AESGCM(_key("totp-secret")).decrypt(raw[:12], raw[12:], b"totp").decode()
    except (InvalidTag, ValueError):
        return ""  # SECRET_KEY changed or the row was tampered with: no code matches


def new_token() -> str:
    return secrets.token_urlsafe(32)


def sha256(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def safe_eq(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


def new_totp_secret() -> str:
    return pyotp.random_base32()


def totp_step(secret: str, code: str, after: int = 0) -> int | None:
    """The 30-second time step this code belongs to (allowing one step of clock
    drift), or None. Steps at or before `after` are refused, so a code that was
    already used can't be replayed."""
    code = (code or "").strip().replace(" ", "")
    if not secret or not code.isdigit() or len(code) != 6:
        return None
    totp, now = pyotp.TOTP(secret), int(time.time() // 30)
    hit = None
    for step in (now - 1, now, now + 1):
        if hmac.compare_digest(totp.at(step * 30), code) and step > after:
            hit = step
    return hit


def check_totp(secret: str, code: str) -> bool:
    return totp_step(secret, code) is not None


# --- recovery codes ----------------------------------------------------------
_RC_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"  # no look-alikes (i, l, o, 0, 1)


def new_recovery_codes(n: int = 10) -> list[str]:
    def one():
        s = "".join(secrets.choice(_RC_ALPHABET) for _ in range(10))
        return f"{s[:5]}-{s[5:]}"
    return [one() for _ in range(n)]


def new_email_code() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"


def email_code_digest(email: str, purpose: str, code: str) -> str:
    """Keyed hash of a mailed code, bound to the address and what it's for."""
    msg = f"{purpose}:{email.lower()}:{code.strip()}".encode()
    return hmac.new(_key("email-code"), msg, hashlib.sha256).hexdigest()


def recovery_digest(code: str) -> str:
    norm = "".join(ch for ch in code.lower() if ch.isalnum())
    return hmac.new(_key("recovery-code"), norm.encode(), hashlib.sha256).hexdigest()


class FailureLimiter:
    """In-memory sliding window of failures per key, for a single process
    (development and tests). With REDIS_URL set, RedisLimiter is used instead."""

    def __init__(self):
        self._events: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def _prune(self, key, window):
        now = time.time()
        q = [t for t in self._events.get(key, []) if now - t < window]
        if q:
            self._events[key] = q
        else:
            self._events.pop(key, None)  # keeps the table from growing forever
        return q

    def blocked(self, key: str, limit: int, window: int) -> bool:
        with self._lock:
            return len(self._prune(key, window)) >= limit

    def fail(self, key: str, window: int) -> None:
        with self._lock:
            q = self._prune(key, window)
            q.append(time.time())
            self._events[key] = q

    def clear(self, key: str) -> None:
        with self._lock:
            self._events.pop(key, None)


class RedisLimiter:
    """The same sliding window kept in Redis (one sorted set of timestamps per
    key), so every API container counts the same failures."""

    def __init__(self, r):
        self.r = r

    def blocked(self, key: str, limit: int, window: int) -> bool:
        k, now = "rl:" + key, time.time()
        p = self.r.pipeline()
        p.zremrangebyscore(k, 0, now - window)
        p.zcard(k)
        return p.execute()[1] >= limit

    def fail(self, key: str, window: int) -> None:
        k, now = "rl:" + key, time.time()
        p = self.r.pipeline()
        p.zadd(k, {f"{now:.6f}:{secrets.token_hex(4)}": now})
        p.zremrangebyscore(k, 0, now - window)
        p.expire(k, int(window) + 1)
        p.execute()

    def clear(self, key: str) -> None:
        self.r.delete("rl:" + key)


class Limiter:
    """Picks Redis when it's configured, else the in-process window."""

    def __init__(self):
        self._mem = FailureLimiter()

    @property
    def _events(self):  # the in-process table, which tests reset between runs
        return self._mem._events

    def _backend(self):
        r = shared.redis_client()
        return RedisLimiter(r) if r is not None else self._mem

    def blocked(self, key: str, limit: int, window: int) -> bool:
        return self._backend().blocked(key, limit, window)

    def fail(self, key: str, window: int) -> None:
        self._backend().fail(key, window)

    def clear(self, key: str) -> None:
        self._backend().clear(key)


limiter = Limiter()
