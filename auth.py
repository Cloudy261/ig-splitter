"""Optional password login (no passkeys, because those need HTTPS).

* The password is stored only as a PBKDF2-HMAC-SHA256 hash (1,000,000 iterations by
  default; the count is part of the hash string, so it can be raised later).
* 5 failed attempts from one IP lock that IP out for 15 minutes; every further failure
  locks it again, until a successful login.
* Sessions live in memory for 30 days (a restart logs everyone out), referenced by a random
  token in an HttpOnly, SameSite=Strict cookie.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import math
import secrets
import threading
import time

ITERATIONS = 1_000_000
MAX_FAILURES = 5
LOCK_SECONDS = 15 * 60
SESSION_SECONDS = 30 * 24 * 60 * 60
COOKIE = "ig_session"
_PREFIX = "pbkdf2-sha256"


class LoginError(Exception):
    def __init__(self, message: str, status: int):
        super().__init__(message)
        self.status = status


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def hash_password(password: str, iterations: int = ITERATIONS) -> str:
    """`pbkdf2-sha256:<iterations>:<salt>:<hash>`, no `$`, so it is safe in .env files."""
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return f"{_PREFIX}:{iterations}:{_b64(salt)}:{_b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    _, iterations, salt, digest = _parse(stored)
    actual = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations, len(digest))
    return hmac.compare_digest(actual, digest)


def _parse(stored: str) -> tuple[str, int, bytes, bytes]:
    try:
        prefix, iterations, salt, digest = stored.strip().split(":")
        if prefix != _PREFIX or int(iterations) < 100_000:
            raise ValueError
        return prefix, int(iterations), _unb64(salt), _unb64(digest)
    except ValueError:
        raise ValueError("Not a valid password hash; create one with: python3 app.py --hash-password") from None


class Auth:
    """Login state of one server. Without a stored hash, everything is open (local use)."""

    def __init__(self, stored_hash: str | None):
        self.stored = stored_hash.strip() if stored_hash else None
        if self.stored:
            _parse(self.stored)  # fail at startup, not at the first login
        self._sessions: dict[str, float] = {}  # token -> expiry
        self._failures: dict[str, tuple[int, float]] = {}  # ip -> (failed attempts, locked until)
        self._lock = threading.Lock()  # one hash at a time keeps the CPU load bounded

    @property
    def enabled(self) -> bool:
        return bool(self.stored)

    def valid(self, token: str | None) -> bool:
        if not self.enabled:
            return True
        expiry = self._sessions.get(token or "")
        if expiry and expiry > time.time():
            return True
        self._sessions.pop(token or "", None)
        return False

    def login(self, ip: str, password: str) -> str:
        """Session token for a correct password; raises LoginError otherwise."""
        with self._lock:
            now = time.time()
            count, locked_until = self._failures.get(ip, (0, 0.0))
            if locked_until > now:
                minutes = math.ceil((locked_until - now) / 60)
                raise LoginError(f"Too many attempts. Try again in {minutes} min.", 429)
            if not verify_password(password, self.stored):
                count += 1
                locked = count >= MAX_FAILURES
                self._failures[ip] = (count, now + LOCK_SECONDS if locked else 0.0)
                if locked:
                    raise LoginError(f"Too many attempts. Try again in {LOCK_SECONDS // 60} min.", 429)
                raise LoginError("Wrong password", 401)
            self._failures.pop(ip, None)
            self._sessions = {t: e for t, e in self._sessions.items() if e > now}  # drop expired
            token = secrets.token_urlsafe(32)
            self._sessions[token] = now + SESSION_SECONDS
            return token

    def logout(self, token: str | None) -> None:
        self._sessions.pop(token or "", None)
