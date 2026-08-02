"""Bounded, monotonic, in-memory login-attempt limiting."""

from __future__ import annotations

import hashlib
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from threading import Lock

Clock = Callable[[], float]
MAX_TRACKED_CLIENTS = 256
GLOBAL_ATTEMPT_MULTIPLIER = 16


@dataclass(slots=True)
class _AttemptRecord:
    attempts: list[float]
    last_seen: float


class LoginAttemptLimiter:
    """Track only keyed digests of client identifiers with strict memory caps."""

    def __init__(
        self,
        *,
        attempt_limit: int,
        window_seconds: float,
        clock: Clock = time.monotonic,
        digest_key: bytes | None = None,
        maximum_clients: int = MAX_TRACKED_CLIENTS,
    ) -> None:
        if attempt_limit <= 0 or window_seconds <= 0 or maximum_clients <= 0:
            raise ValueError("attempt-limiter bounds must be positive")
        self._attempt_limit = attempt_limit
        self._window_seconds = window_seconds
        self._clock = clock
        self._digest_key = secrets.token_bytes(32) if digest_key is None else digest_key
        if not self._digest_key:
            raise ValueError("attempt-limiter digest key must not be empty")
        self._maximum_clients = maximum_clients
        self._records: dict[bytes, _AttemptRecord] = {}
        self._global_attempts: list[float] = []
        self._global_attempt_limit = attempt_limit * GLOBAL_ATTEMPT_MULTIPLIER
        self._lock = Lock()

    def begin_attempt(self, client_identifier: str) -> bool:
        """Atomically reserve one attempt or reject an already limited client."""
        digest = self._digest(client_identifier)
        with self._lock:
            now = self._clock()
            self._cleanup(now)
            if len(self._global_attempts) >= self._global_attempt_limit:
                return False
            record = self._records.get(digest)
            if record is None:
                if len(self._records) >= self._maximum_clients:
                    oldest = min(
                        self._records,
                        key=lambda key: self._records[key].last_seen,
                    )
                    del self._records[oldest]
                record = _AttemptRecord([], now)
                self._records[digest] = record
            cutoff = now - self._window_seconds
            record.attempts[:] = [stamp for stamp in record.attempts if stamp > cutoff]
            record.last_seen = now
            if len(record.attempts) >= self._attempt_limit:
                return False
            record.attempts.append(now)
            self._global_attempts.append(now)
            return True

    def clear(self, client_identifier: str) -> None:
        with self._lock:
            self._records.pop(self._digest(client_identifier), None)

    @property
    def tracked_client_count(self) -> int:
        with self._lock:
            self._cleanup(self._clock())
            return len(self._records)

    def attempt_count(self, client_identifier: str) -> int:
        digest = self._digest(client_identifier)
        with self._lock:
            now = self._clock()
            self._cleanup(now)
            record = self._records.get(digest)
            return 0 if record is None else len(record.attempts)

    def _cleanup(self, now: float) -> None:
        cutoff = now - self._window_seconds
        self._global_attempts[:] = [
            stamp for stamp in self._global_attempts if stamp > cutoff
        ]
        expired = []
        for digest, record in self._records.items():
            record.attempts[:] = [stamp for stamp in record.attempts if stamp > cutoff]
            if not record.attempts:
                expired.append(digest)
        for digest in expired:
            del self._records[digest]

    def _digest(self, client_identifier: str) -> bytes:
        return hashlib.blake2b(
            client_identifier.encode("utf-8", errors="replace"),
            digest_size=16,
            key=self._digest_key,
        ).digest()


class MutationAttemptLimiter(LoginAttemptLimiter):
    """A separately instantiated bounded limiter for WLED mutation attempts."""
