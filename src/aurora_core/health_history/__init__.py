"""Isolated production foundations for bounded health-history storage.

This package is intentionally not imported by a current runtime entry point.
"""

from aurora_core.health_history.models import (
    AlertKind,
    AlertLifecycle,
    AlertScope,
    ComponentName,
    HealthHistoryStatus,
    LifecycleEvent,
    SampleKind,
    SamplingGapPhase,
)
from aurora_core.health_history.projection import (
    ComponentProjection,
    HealthProjection,
    ProjectionError,
    ProjectionRejection,
    project_health_report,
)
from aurora_core.health_history.store import HealthHistoryStore

__all__ = [
    "AlertKind",
    "AlertLifecycle",
    "AlertScope",
    "ComponentName",
    "ComponentProjection",
    "HealthHistoryStatus",
    "HealthHistoryStore",
    "HealthProjection",
    "LifecycleEvent",
    "ProjectionError",
    "ProjectionRejection",
    "SampleKind",
    "SamplingGapPhase",
    "project_health_report",
]
