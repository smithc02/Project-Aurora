"""Concurrent, single-flight health aggregation service."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import replace
from threading import Lock

from aurora_core.config.models import AuroraSettings
from aurora_core.dashboard.collectors import CollectorSpec, collect_components
from aurora_core.dashboard.models import (
    ComponentHealth,
    HealthReport,
    overall_status,
    utc_now_iso,
)

Clock = Callable[[], float]
_STARTED_AT = time.monotonic()


def collect_health(
    settings: AuroraSettings,
    collectors: tuple[CollectorSpec, ...] | None = None,
    *,
    service_uptime_seconds: float | None = None,
) -> HealthReport:
    """Collect one isolated read-only snapshot without caching it."""
    components = collect_components(settings, collectors)
    uptime = (
        time.monotonic() - _STARTED_AT
        if service_uptime_seconds is None
        else service_uptime_seconds
    )
    return HealthReport(
        status=overall_status(components),
        checked_at=utc_now_iso(),
        service_uptime_seconds=round(uptime, 1),
        components=components,
    )


class HealthService:
    """Cache one snapshot and prevent overlapping collector sweeps."""

    def __init__(
        self,
        settings: AuroraSettings,
        collectors: tuple[CollectorSpec, ...] | None = None,
        *,
        clock: Clock = time.monotonic,
    ) -> None:
        self._settings = settings
        self._collectors = collectors
        self._clock = clock
        self._started_at = clock()
        self._collection_lock = Lock()
        self._state_lock = Lock()
        self._cached_report: HealthReport | None = None
        self._cached_at: float | None = None
        self._invalidation_generation = 0
        self._last_successes: dict[str, str] = {}

    def get_health(self) -> HealthReport:
        """Return a recent snapshot, collecting at most one at a time."""
        with self._collection_lock:
            now = self._clock()
            with self._state_lock:
                if (
                    self._cached_report is not None
                    and self._cached_at is not None
                    and now - self._cached_at < self._settings.dashboard.refresh_seconds
                ):
                    return self._cached_report
                collection_generation = self._invalidation_generation

            report = collect_health(
                self._settings,
                self._collectors,
                service_uptime_seconds=now - self._started_at,
            )
            components = tuple(
                self._with_last_success(component) for component in report.components
            )
            report = replace(
                report,
                status=overall_status(components),
                components=components,
            )
            with self._state_lock:
                if collection_generation == self._invalidation_generation:
                    self._cached_report = report
                    self._cached_at = self._clock()
            return report

    def invalidate(self) -> None:
        """Discard the cache without polling or waiting for an active sweep."""
        with self._state_lock:
            self._invalidation_generation += 1
            self._cached_report = None
            self._cached_at = None

    def _with_last_success(self, component: ComponentHealth) -> ComponentHealth:
        if component.last_successful_at is not None:
            self._last_successes[component.name] = component.last_successful_at
            return component
        previous = self._last_successes.get(component.name)
        if previous is None:
            return component
        return replace(component, last_successful_at=previous)
