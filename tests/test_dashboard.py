"""Tests for the read-only health dashboard."""

from __future__ import annotations

from aurora_core.dashboard.models import (
    ComponentHealth,
    HealthReport,
    HealthStatus,
    overall_status,
)
from aurora_core.dashboard.server import _render_page
from aurora_core.dashboard.settings import DashboardSettings


def _component(status: HealthStatus) -> ComponentHealth:
    return ComponentHealth(
        name="example",
        status=status,
        message="test",
        checked_at="2026-01-01T00:00:00+00:00",
        latency_ms=1.5,
        details={"value": 1},
    )


def test_overall_status_uses_worst_component() -> None:
    assert overall_status((_component(HealthStatus.HEALTHY),)) is HealthStatus.HEALTHY
    assert (
        overall_status(
            (_component(HealthStatus.HEALTHY), _component(HealthStatus.DEGRADED))
        )
        is HealthStatus.DEGRADED
    )
    assert (
        overall_status(
            (
                _component(HealthStatus.HEALTHY),
                _component(HealthStatus.UNAVAILABLE),
            )
        )
        is HealthStatus.UNAVAILABLE
    )


def test_health_report_is_json_serializable_shape() -> None:
    report = HealthReport(
        status=HealthStatus.HEALTHY,
        checked_at="2026-01-01T00:00:00+00:00",
        service_uptime_seconds=2.0,
        components=(_component(HealthStatus.HEALTHY),),
    ).to_dict()
    assert report["status"] == HealthStatus.HEALTHY
    assert report["components"][0]["details"] == {"value": 1}


def test_page_renders_component_and_escapes_text() -> None:
    report = HealthReport(
        status=HealthStatus.DEGRADED,
        checked_at="2026-01-01T00:00:00+00:00",
        service_uptime_seconds=2.0,
        components=(
            ComponentHealth(
                name="wled",
                status=HealthStatus.DEGRADED,
                message="<unsafe>",
                checked_at="2026-01-01T00:00:00+00:00",
                latency_ms=1.0,
                details={},
            ),
        ),
    ).to_dict()
    page = _render_page(report, 5)
    assert "Project Aurora" in page
    assert "&lt;unsafe&gt;" in page
    assert '<meta http-equiv="refresh" content="5">' in page


def test_default_deployment_values() -> None:
    settings = DashboardSettings()
    assert settings.capture_device == "/dev/video0"
    assert settings.expected_led_count == 282
    assert settings.expected_skipped_leds == 16
    assert settings.expected_active_leds == 266
