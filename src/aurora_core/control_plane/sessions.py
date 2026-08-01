"""Thread-safe, bounded, in-memory control-plane sessions."""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from threading import Lock

Clock = Callable[[], float]
TokenFactory = Callable[[int], str]
SESSION_TOKEN_BYTES = 32
CSRF_TOKEN_BYTES = 32
_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{32,256}")


class SessionLookupStatus(StrEnum):
    VALID = "valid"
    INVALID = "invalid"
    EXPIRED = "expired"


@dataclass(frozen=True, slots=True)
class SessionContext:
    """Safe session state used by protected renderers and CSRF validation."""

    username: str
    csrf_token: str
    expires_in_seconds: float


@dataclass(frozen=True, slots=True)
class SessionLookup:
    status: SessionLookupStatus
    session: SessionContext | None = None


@dataclass(frozen=True, slots=True)
class CreatedSession:
    token: str
    session: SessionContext


@dataclass(frozen=True, slots=True)
class _SessionRecord:
    username: str
    csrf_token: str
    created_at: float
    expires_at: float
    sequence: int


class SessionStore:
    """Store only digests of opaque cookie tokens until process exit."""

    def __init__(
        self,
        *,
        ttl_seconds: float,
        maximum_sessions: int,
        clock: Clock = time.monotonic,
        token_factory: TokenFactory = secrets.token_urlsafe,
    ) -> None:
        if ttl_seconds <= 0 or maximum_sessions <= 0:
            raise ValueError("session bounds must be positive")
        self._ttl_seconds = ttl_seconds
        self._maximum_sessions = maximum_sessions
        self._clock = clock
        self._token_factory = token_factory
        self._sessions: dict[bytes, _SessionRecord] = {}
        self._sequence = 0
        self._lock = Lock()

    def create(self, username: str) -> CreatedSession:
        """Create a fresh session, evicting the oldest if the cap is reached."""
        with self._lock:
            now = self._clock()
            self._purge_expired(now)
            while len(self._sessions) >= self._maximum_sessions:
                oldest = min(
                    self._sessions,
                    key=lambda digest: self._sessions[digest].sequence,
                )
                del self._sessions[oldest]
            token = self._unique_token()
            csrf_token = self._new_token(CSRF_TOKEN_BYTES)
            self._sequence += 1
            record = _SessionRecord(
                username=username,
                csrf_token=csrf_token,
                created_at=now,
                expires_at=now + self._ttl_seconds,
                sequence=self._sequence,
            )
            self._sessions[_digest(token)] = record
            return CreatedSession(token, self._context(record, now))

    def resolve(self, token: str) -> SessionLookup:
        """Resolve one opaque token without ever retaining its plaintext value."""
        if not opaque_token_is_valid(token):
            return SessionLookup(SessionLookupStatus.INVALID)
        with self._lock:
            now = self._clock()
            digest = _digest(token)
            record = self._sessions.get(digest)
            if record is None:
                self._purge_expired(now)
                return SessionLookup(SessionLookupStatus.INVALID)
            if now >= record.expires_at:
                del self._sessions[digest]
                self._purge_expired(now)
                return SessionLookup(SessionLookupStatus.EXPIRED)
            self._purge_expired(now)
            return SessionLookup(
                SessionLookupStatus.VALID,
                self._context(record, now),
            )

    def invalidate(self, token: str) -> bool:
        if not opaque_token_is_valid(token):
            return False
        with self._lock:
            return self._sessions.pop(_digest(token), None) is not None

    @property
    def active_count(self) -> int:
        with self._lock:
            self._purge_expired(self._clock())
            return len(self._sessions)

    def _unique_token(self) -> str:
        for _ in range(8):
            token = self._new_token(SESSION_TOKEN_BYTES)
            if _digest(token) not in self._sessions:
                return token
        raise RuntimeError("could not allocate a unique session token")

    def _new_token(self, byte_count: int) -> str:
        token = self._token_factory(byte_count)
        if not opaque_token_is_valid(token):
            raise RuntimeError("token source returned an invalid token")
        return token

    def _purge_expired(self, now: float) -> None:
        expired = tuple(
            digest
            for digest, record in self._sessions.items()
            if now >= record.expires_at
        )
        for digest in expired:
            del self._sessions[digest]

    @staticmethod
    def _context(record: _SessionRecord, now: float) -> SessionContext:
        return SessionContext(
            username=record.username,
            csrf_token=record.csrf_token,
            expires_in_seconds=max(0.0, record.expires_at - now),
        )


def csrf_is_valid(session: SessionContext, submitted_token: str) -> bool:
    """Compare one bounded per-session CSRF token in constant time."""
    if not opaque_token_is_valid(submitted_token):
        return False
    return hmac.compare_digest(session.csrf_token, submitted_token)


def opaque_token_is_valid(token: str) -> bool:
    """Return whether a session or CSRF token uses the bounded wire grammar."""
    return _TOKEN_PATTERN.fullmatch(token) is not None


def _digest(token: str) -> bytes:
    return hashlib.sha256(token.encode("ascii")).digest()
