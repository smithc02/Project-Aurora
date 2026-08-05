"""Strict projection from public health schema version 1 to persistent fields."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from aurora_core.dashboard.models import ComponentHealth, HealthReport, HealthStatus
from aurora_core.health_history.models import (
    COMPONENT_ORDER,
    MAX_BOUNDED_COUNTER,
    MAX_COMPONENT_LATENCY_MS,
    MAX_SERVICE_UPTIME_MS,
    MAX_TIMESTAMP_US,
    PROJECTION_DIGEST_BYTES,
    ComponentName,
    HealthHistoryStatus,
    SampleKind,
)
from aurora_core.health_history.reasons import (
    NormalizedReason,
    ReasonDecision,
    normalize_component_reason,
)


class ProjectionRejection(StrEnum):
    INVALID_REPORT = "invalid_report"
    UNKNOWN_SCHEMA = "unknown_schema"
    INVALID_STATUS = "invalid_status"
    INVALID_TIMESTAMP = "invalid_timestamp"
    INVALID_DURATION = "invalid_duration"
    INVALID_COMPONENTS = "invalid_components"
    INVALID_COMPONENT = "invalid_component"
    REASON_REJECTED = "reason_rejected"


class ProjectionError(Exception):
    """Sanitized fail-closed projection error."""

    def __init__(self, reason: ProjectionRejection) -> None:
        super().__init__(reason.value)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class ComponentProjection:
    component: ComponentName
    status: HealthHistoryStatus
    reasons: tuple[NormalizedReason, ...]
    checked_at_utc_us: int
    latency_ms: int
    last_successful_at_utc_us: int | None

    def __post_init__(self) -> None:
        if not isinstance(self.component, ComponentName):
            raise ValueError("invalid_component")
        if not isinstance(self.status, HealthHistoryStatus):
            raise ValueError("invalid_status")
        if (
            type(self.reasons) is not tuple
            or not 1 <= len(self.reasons) <= 3
            or any(not isinstance(reason, NormalizedReason) for reason in self.reasons)
        ):
            raise ValueError("invalid_reasons")
        if len(set(self.reasons)) != len(self.reasons):
            raise ValueError("duplicate_reasons")
        _bounded_integer(self.checked_at_utc_us, MAX_TIMESTAMP_US, "checked_at")
        _bounded_integer(self.latency_ms, MAX_COMPONENT_LATENCY_MS, "latency")
        if self.last_successful_at_utc_us is not None:
            _bounded_integer(
                self.last_successful_at_utc_us,
                MAX_TIMESTAMP_US,
                "last_successful_at",
            )


@dataclass(frozen=True, slots=True)
class HealthProjection:
    schema_version: int
    observed_at_utc_us: int
    recorded_at_utc_us: int
    overall_status: HealthHistoryStatus
    service_uptime_ms: int
    sample_kind: SampleKind
    missed_intervals: int
    components: tuple[ComponentProjection, ...]
    digest: bytes

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("invalid_schema_version")
        _bounded_integer(self.observed_at_utc_us, MAX_TIMESTAMP_US, "observed_at")
        _bounded_integer(self.recorded_at_utc_us, MAX_TIMESTAMP_US, "recorded_at")
        if not isinstance(self.overall_status, HealthHistoryStatus):
            raise ValueError("invalid_overall_status")
        _bounded_integer(
            self.service_uptime_ms, MAX_SERVICE_UPTIME_MS, "service_uptime"
        )
        if not isinstance(self.sample_kind, SampleKind):
            raise ValueError("invalid_sample_kind")
        _bounded_integer(self.missed_intervals, MAX_BOUNDED_COUNTER, "missed_intervals")
        if type(self.components) is not tuple or any(
            type(component) is not ComponentProjection for component in self.components
        ):
            raise ValueError("invalid_components")
        if (
            tuple(component.component for component in self.components)
            != COMPONENT_ORDER
        ):
            raise ValueError("invalid_component_order")
        if (
            type(self.digest) is not bytes
            or len(self.digest) != PROJECTION_DIGEST_BYTES
        ):
            raise ValueError("invalid_digest")


def project_health_report(
    report: HealthReport,
    *,
    recorded_at: datetime,
    sample_kind: SampleKind = SampleKind.HEARTBEAT,
    missed_intervals: int = 0,
) -> HealthProjection:
    """Validate and project a complete current health report atomically."""
    if type(report) is not HealthReport:
        raise ProjectionError(ProjectionRejection.INVALID_REPORT)
    if type(report.schema_version) is not int or report.schema_version != 1:
        raise ProjectionError(ProjectionRejection.UNKNOWN_SCHEMA)
    status = _status(report.status)
    observed_at = _timestamp_from_text(report.checked_at)
    recorded_at_us = _timestamp_from_datetime(recorded_at)
    uptime = _duration_ms(report.service_uptime_seconds, MAX_SERVICE_UPTIME_MS)
    if not isinstance(sample_kind, SampleKind):
        raise ProjectionError(ProjectionRejection.INVALID_REPORT)
    try:
        _bounded_integer(missed_intervals, MAX_BOUNDED_COUNTER, "missed_intervals")
    except ValueError as error:
        raise ProjectionError(ProjectionRejection.INVALID_DURATION) from error
    if type(report.components) is not tuple or any(
        type(component) is not ComponentHealth for component in report.components
    ):
        raise ProjectionError(ProjectionRejection.INVALID_COMPONENTS)
    by_name: dict[str, ComponentHealth] = {}
    for component in report.components:
        if component.name not in {name.value for name in COMPONENT_ORDER}:
            raise ProjectionError(ProjectionRejection.INVALID_COMPONENT)
        if component.name in by_name:
            raise ProjectionError(ProjectionRejection.INVALID_COMPONENTS)
        by_name[component.name] = component
    if set(by_name) != {name.value for name in COMPONENT_ORDER}:
        raise ProjectionError(ProjectionRejection.INVALID_COMPONENTS)
    components = tuple(
        _project_component(name, by_name[name.value]) for name in COMPONENT_ORDER
    )
    canonical = _canonical_bytes(
        observed_at=observed_at,
        status=status,
        uptime=uptime,
        components=components,
    )
    return HealthProjection(
        schema_version=1,
        observed_at_utc_us=observed_at,
        recorded_at_utc_us=recorded_at_us,
        overall_status=status,
        service_uptime_ms=uptime,
        sample_kind=sample_kind,
        missed_intervals=missed_intervals,
        components=components,
        digest=hashlib.sha256(canonical).digest(),
    )


def _project_component(
    expected: ComponentName, component: ComponentHealth
) -> ComponentProjection:
    status = _status(component.status)
    if type(component.details) is not dict:
        raise ProjectionError(ProjectionRejection.REASON_REJECTED)
    result = normalize_component_reason(
        schema_version=1,
        component=expected.value,
        status=status.value,
        details=component.details,
        message=component.message,
    )
    if result.decision is not ReasonDecision.ACCEPTED:
        raise ProjectionError(ProjectionRejection.REASON_REJECTED)
    try:
        checked_at = _timestamp_from_text(component.checked_at)
        last_successful = (
            None
            if component.last_successful_at is None
            else _timestamp_from_text(component.last_successful_at)
        )
        latency = _rounded_milliseconds(component.latency_ms, MAX_COMPONENT_LATENCY_MS)
    except ProjectionError:
        raise
    return ComponentProjection(
        component=expected,
        status=status,
        reasons=result.reasons,
        checked_at_utc_us=checked_at,
        latency_ms=latency,
        last_successful_at_utc_us=last_successful,
    )


def _status(value: object) -> HealthHistoryStatus:
    if not isinstance(value, HealthStatus):
        raise ProjectionError(ProjectionRejection.INVALID_STATUS)
    return HealthHistoryStatus(value.value)


def _timestamp_from_text(value: object) -> int:
    if not isinstance(value, str) or len(value) > 64:
        raise ProjectionError(ProjectionRejection.INVALID_TIMESTAMP)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ProjectionError(ProjectionRejection.INVALID_TIMESTAMP) from error
    return _timestamp_from_datetime(parsed)


def _timestamp_from_datetime(value: object) -> int:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ProjectionError(ProjectionRejection.INVALID_TIMESTAMP)
    try:
        normalized = value.astimezone(UTC)
        epoch = datetime(1970, 1, 1, tzinfo=UTC)
        delta = normalized - epoch
        result = (
            delta.days * 86_400_000_000 + delta.seconds * 1_000_000 + delta.microseconds
        )
        _bounded_integer(result, MAX_TIMESTAMP_US, "timestamp")
    except (OverflowError, ValueError) as error:
        raise ProjectionError(ProjectionRejection.INVALID_TIMESTAMP) from error
    return result


def _duration_ms(value: object, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ProjectionError(ProjectionRejection.INVALID_DURATION)
    converted = float(value)
    if not math.isfinite(converted) or converted < 0:
        raise ProjectionError(ProjectionRejection.INVALID_DURATION)
    rounded = round(converted * 1000)
    if rounded > maximum:
        raise ProjectionError(ProjectionRejection.INVALID_DURATION)
    return rounded


def _rounded_milliseconds(value: object, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ProjectionError(ProjectionRejection.INVALID_DURATION)
    converted = float(value)
    if not math.isfinite(converted) or not 0 <= converted <= maximum:
        raise ProjectionError(ProjectionRejection.INVALID_DURATION)
    return round(converted)


def _bounded_integer(value: object, maximum: int, name: str) -> None:
    if type(value) is not int or not 0 <= value <= maximum:
        raise ValueError(f"invalid_{name}")


def _canonical_bytes(
    *,
    observed_at: int,
    status: HealthHistoryStatus,
    uptime: int,
    components: tuple[ComponentProjection, ...],
) -> bytes:
    fields: list[object] = [
        1,
        observed_at,
        status.value,
        uptime,
    ]
    fields.extend(
        [
            component.component.value,
            component.status.value,
            [reason.value for reason in component.reasons],
            component.checked_at_utc_us,
            component.latency_ms,
            component.last_successful_at_utc_us,
        ]
        for component in components
    )
    return json.dumps(fields, ensure_ascii=True, separators=(",", ":")).encode("ascii")
