"""Neutral process-local serialization for Aurora device mutations."""

from __future__ import annotations

from threading import RLock


class ControlMutationGate:
    """Provide reentrant, nonblocking ownership with deterministic release."""

    def __init__(self) -> None:
        self._lock = RLock()

    def acquire(self) -> bool:
        """Acquire immediately or report contention without waiting."""
        return self._lock.acquire(blocking=False)

    def release(self) -> None:
        """Release one acquisition owned by the current thread."""
        self._lock.release()
