"""Deterministic, conservative runtime-health aggregation."""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from aurora_core.runtime.models import (
    ComponentHealthReport,
    ComponentHealthState,
    ComponentId,
    ComponentLifecycleState,
    ComponentPlan,
    OverallHealthState,
)


def derive_overall_health(
    plans: Iterable[ComponentPlan],
    reports: Mapping[ComponentId, ComponentHealthReport],
    lifecycle: Mapping[ComponentId, ComponentLifecycleState],
) -> OverallHealthState:
    """Derive health conservatively; missing reports and failures are unsafe."""
    enabled = [plan for plan in plans if plan.enabled]
    if not enabled:
        return OverallHealthState.DISABLED
    states: list[ComponentHealthState] = []
    for plan in enabled:
        if lifecycle.get(plan.component_id) is ComponentLifecycleState.FAILED:
            return OverallHealthState.UNHEALTHY
        report = reports.get(plan.component_id)
        states.append(ComponentHealthState.UNKNOWN if report is None else report.state)
    if ComponentHealthState.UNHEALTHY in states:
        return OverallHealthState.UNHEALTHY
    if ComponentHealthState.DEGRADED in states:
        return OverallHealthState.DEGRADED
    if ComponentHealthState.UNKNOWN in states:
        return OverallHealthState.UNKNOWN
    return OverallHealthState.HEALTHY
