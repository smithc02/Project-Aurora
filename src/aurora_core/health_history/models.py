"""Strict code-owned types for production health-history records."""

from __future__ import annotations

from enum import IntEnum, StrEnum
from typing import Final

APPLICATION_ID: Final = 0x41555248
SCHEMA_VERSION: Final = 1
PAGE_SIZE_BYTES: Final = 4096
AUTO_VACUUM_INCREMENTAL: Final = 2
BUSY_TIMEOUT_MILLISECONDS: Final = 250
PROJECTION_DIGEST_BYTES: Final = 32
MAX_SCHEMA_OBJECTS: Final = 64
MAX_DATABASE_BYTES: Final = 64 * 1024 * 1024
MAX_WAL_BYTES: Final = 4 * 1024 * 1024
MAX_SHARED_MEMORY_BYTES: Final = 1024 * 1024
MAX_TIMESTAMP_US: Final = 2**63 - 1
MAX_SERVICE_UPTIME_MS: Final = 31_536_000_000_000
MAX_COMPONENT_LATENCY_MS: Final = 300_000
MAX_BOUNDED_COUNTER: Final = 65_535
MAX_OBSERVATION_SEQUENCE: Final = 2**63 - 1
REPLAY_LEDGER_CAPACITY: Final = 64
MAX_SCHEMA_VERSION: Final = 2_147_483_647


class DatabaseIdentity(IntEnum):
    """Fixed application identities that production opening distinguishes."""

    AURORA_HISTORY = APPLICATION_ID
    M18_SYNTHETIC_BENCHMARK = 0x4D313842


class ComponentName(StrEnum):
    WLED = "wled"
    HYPERHDR = "hyperhdr"
    CAPTURE = "capture"
    RASPBERRY_PI = "raspberry_pi"


COMPONENT_ORDER: Final = tuple(ComponentName)


class HealthHistoryStatus(StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"


class SampleKind(StrEnum):
    TRANSITION = "transition"
    HEARTBEAT = "heartbeat"
    STARTUP_GAP = "startup_gap"
    CLOCK_DISCONTINUITY = "clock_discontinuity"


class AlertScope(StrEnum):
    OVERALL = "overall"
    WLED = "wled"
    HYPERHDR = "hyperhdr"
    CAPTURE = "capture"
    RASPBERRY_PI = "raspberry_pi"
    SAMPLING = "sampling"


class AlertKind(StrEnum):
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"
    SAMPLING_GAP = "sampling_gap"


class AlertLifecycle(StrEnum):
    OPEN = "open"
    ACKNOWLEDGED = "acknowledged"
    RECOVERED = "recovered"
    ARCHIVED = "archived"


class LifecycleEvent(StrEnum):
    OPENED = "opened"
    OCCURRENCE_UPDATED = "occurrence_updated"
    ACKNOWLEDGED = "acknowledged"
    RECOVERED = "recovered"
    ARCHIVED = "archived"


class SamplingGapPhase(StrEnum):
    CLEAR = "clear"
    CANDIDATE_ONE = "candidate_one"
    ACTIVE = "active"
    RECOVERY_ONE = "recovery_one"
