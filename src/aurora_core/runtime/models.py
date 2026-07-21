"""Immutable, sanitized domain models for the hardware-free runtime."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class ComponentId(StrEnum):
    """Known runtime components in their deterministic operational order."""

    CAPTURE_DEVICE = "capture_device"
    HYPERHDR = "hyperhdr"
    WLED = "wled"
    DDP = "ddp"
    MQTT = "mqtt"


COMPONENT_ORDER: tuple[ComponentId, ...] = tuple(ComponentId)


class ComponentLifecycleState(StrEnum):
    CREATED = "created"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    STOPPED = "stopped"
    FAILED = "failed"


class ComponentHealthState(StrEnum):
    DISABLED = "disabled"
    UNKNOWN = "unknown"
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"


class OverallHealthState(StrEnum):
    DISABLED = "disabled"
    UNKNOWN = "unknown"
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"


@dataclass(frozen=True, slots=True)
class ComponentPlan:
    """Safe planning facts; it deliberately contains no endpoint details or secrets."""

    component_id: ComponentId
    enabled: bool
    configured: bool
    initial_lifecycle: ComponentLifecycleState
    initial_health: ComponentHealthState


@dataclass(frozen=True, slots=True)
class RuntimePlan:
    """An immutable configuration snapshot for exactly one controller lifetime."""

    components: tuple[ComponentPlan, ...]
    lighting_zone_count: int
    led_layout_configured: bool


@dataclass(frozen=True, slots=True)
class ComponentHealthReport:
    """A concise public health report, intentionally free of raw exceptions."""

    component_id: ComponentId
    state: ComponentHealthState
    reason_code: str | None = None
    message: str | None = None


@dataclass(frozen=True, slots=True)
class RuntimeSnapshot:
    """Immutable runtime state without a copy of settings or diagnostic secrets."""

    lifecycle: tuple[tuple[ComponentId, ComponentLifecycleState], ...]
    health_reports: tuple[ComponentHealthReport, ...]
    overall_health: OverallHealthState
