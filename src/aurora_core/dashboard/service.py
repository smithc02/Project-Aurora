"""Health aggregation service."""

from __future__ import annotations

import time

from aurora_core.dashboard.collectors import (
    collect_capture,
    collect_hyperhdr,
    collect_pi,
    collect_wled,
)
from aurora_core.dashboard.models import HealthReport, overall_status, utc_now_iso
from aurora_core.dashboard.settings import DashboardSettings

_STARTED_AT = time.monotonic()


def collect_health(settings: DashboardSettings) -> HealthReport:
    """Run all bounded read-only collectors and return one report."""
    components = (
        collect_wled(settings),
        collect_hyperhdr(settings),
        collect_capture(settings),
        collect_pi(),
    )
    return HealthReport(
        status=overall_status(components),
        checked_at=utc_now_iso(),
        service_uptime_seconds=round(time.monotonic() - _STARTED_AT, 1),
        components=components,
    )
