"""Stable data models for the read-only health dashboard."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum


class HealthStatus(StrEnum):
    """Health severity ordered from best to worst."""

    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class ComponentHealth:
    """One bounded health observation."""

    name: str
    status: HealthStatus
    message: str
    checked_at: str
    latency_ms: float
    details: dict[str, object] = field(default_factory=dict)
    last_successful_at: str | None = None


@dataclass(frozen=True, slots=True)
class HealthReport:
    """Aggregated dashboard response."""

    status: HealthStatus
    checked_at: str
    service_uptime_seconds: float
    components: tuple[ComponentHealth, ...]
    schema_version: int = 1

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-serializable representation."""
        return asdict(self)


def utc_now_iso() -> str:
    """Return an RFC3339-style UTC timestamp."""
    return datetime.now(UTC).isoformat()


def overall_status(components: tuple[ComponentHealth, ...]) -> HealthStatus:
    """Return the worst component status."""
    if not components:
        return HealthStatus.UNAVAILABLE
    statuses = {component.status for component in components}
    if HealthStatus.UNAVAILABLE in statuses:
        return HealthStatus.UNAVAILABLE
    if HealthStatus.DEGRADED in statuses:
        return HealthStatus.DEGRADED
    return HealthStatus.HEALTHY
