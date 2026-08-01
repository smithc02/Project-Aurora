"""Tests for the Milestone 13 read-only portal shell and routes."""

from __future__ import annotations

import io
import json
from http import HTTPStatus
from types import SimpleNamespace

import pytest

from aurora_core.config import load_settings
from aurora_core.dashboard.assets import PORTAL_CSS_PATH
from aurora_core.dashboard.collectors import CollectorSpec
from aurora_core.dashboard.models import ComponentHealth, HealthReport, HealthStatus
from aurora_core.dashboard.portal import PORTAL_PATHS, render_portal
from aurora_core.dashboard.server import DashboardHandler
from aurora_core.dashboard.service import HealthService


def _component(
    name: str,
    status: HealthStatus = HealthStatus.HEALTHY,
    *,
    message: str = "Observation is healthy",
    details: dict[str, object] | None = None,
    last_successful_at: str | None = "2026-01-01T00:00:00+00:00",
) -> ComponentHealth:
    return ComponentHealth(
        name=name,
        status=status,
        message=message,
        checked_at="2026-01-01T00:00:00+00:00",
        latency_ms=2.5,
        details={} if details is None else details,
        last_successful_at=last_successful_at,
    )


def _report(
    status: HealthStatus = HealthStatus.HEALTHY,
    *,
    components: tuple[ComponentHealth, ...] | None = None,
) -> HealthReport:
    if components is None:
        components = (
            _component(
                "wled",
                details={
                    "info_reason_code": "validated",
                    "state_reason_code": "validated",
                    "firmware_version": "0.15.0",
                    "uptime_seconds": 120,
                    "reported_led_count": 8,
                    "expected_led_count": 8,
                    "expected_active_led_count": 6,
                    "expected_skipped_leds": 2,
                    "led_count_matches": True,
                    "estimated_current_milliamps": 120,
                    "current_limit_milliamps": 850,
                    "brightness": 64,
                    "output_on": True,
                },
            ),
            _component(
                "hyperhdr",
                details={
                    "reason_code": "validated",
                    "server_info_received": True,
                    "hdr_mode_enabled": True,
                    "instance_running": True,
                    "grabber_active": True,
                    "led_output_active": True,
                },
            ),
            _component(
                "capture",
                details={
                    "reason_code": "validated",
                    "device_node_present": True,
                    "character_device": True,
                    "v4l2_registered": True,
                    "process_read_access": True,
                    "device_name": "Generic capture device",
                    "activity_source": "HyperHDR serverinfo",
                    "grabber_active": True,
                },
            ),
            _component(
                "raspberry_pi",
                details={
                    "cpu_temperature_c": 51.2,
                    "cpu_temperature_warning_c": 80.0,
                    "load_average_1m": 0.1,
                    "load_average_5m": 0.2,
                    "load_average_15m": 0.3,
                    "logical_cpu_count": 4,
                    "memory_used_percent": 32.0,
                    "memory_warning_percent": 90.0,
                    "root_storage_used_percent": 45.0,
                    "storage_warning_percent": 90.0,
                    "host_uptime_seconds": 3600,
                },
            ),
        )
    return HealthReport(
        status=status,
        checked_at="2026-01-01T00:00:00+00:00",
        service_uptime_seconds=42.0,
        components=components,
    )


class StubHealthService:
    def __init__(self, report: HealthReport) -> None:
        self.report = report
        self.calls = 0

    def get_health(self) -> HealthReport:
        self.calls += 1
        return self.report


def _handler(service: object) -> DashboardHandler:
    handler = DashboardHandler.__new__(DashboardHandler)
    handler.server = SimpleNamespace(health_service=service, refresh_seconds=5)
    return handler


def _request(service: object, path: str) -> tuple[bytes, str, HTTPStatus]:
    handler = _handler(service)
    responses: list[tuple[bytes, str, HTTPStatus]] = []
    handler._send = lambda body, content_type, status=HTTPStatus.OK: responses.append(  # type: ignore[method-assign]
        (body, content_type, status)
    )
    handler.path = path
    handler.do_GET()
    return responses[-1]


@pytest.mark.parametrize("path", sorted(PORTAL_PATHS))
def test_every_portal_route_renders_from_one_healthy_snapshot(path: str) -> None:
    service = StubHealthService(_report())
    body, content_type, status = _request(service, path)
    page = body.decode()
    assert status is HTTPStatus.OK
    assert content_type == "text/html; charset=utf-8"
    assert "Project Aurora" in page
    assert "Overall healthy" in page
    assert 'aria-label="Primary navigation"' in page
    assert 'aria-current="page"' in page
    assert '<meta name="viewport"' in page
    assert service.calls == 1


def test_overview_contains_existing_component_and_system_health() -> None:
    page = render_portal(_report(), "/", 5)
    for expected in (
        "WLED",
        "Current output",
        "Brightness",
        "HyperHDR",
        "Grabber",
        "LED output",
        "Capture device",
        "Active grabber",
        "Raspberry Pi",
        "CPU temperature",
        "Memory use",
        "Root-storage use",
        "Service uptime",
    ):
        assert expected in page


@pytest.mark.parametrize(
    ("path", "expected_fields"),
    (
        (
            "/wled",
            (
                "Firmware version",
                "WLED uptime",
                "Reported LED count",
                "Expected LED count",
                "LED-count match",
                "Estimated current",
                "Configured current limit",
                "Brightness",
                "Current output",
            ),
        ),
        (
            "/hyperhdr",
            ("Instance", "Grabber", "LED output", "HDR mode"),
        ),
        (
            "/capture",
            (
                "Node present",
                "Character-device validation",
                "V4L2 registration",
                "Process read access",
                "Bounded device name",
                "Grabber activity source",
                "Active grabber",
            ),
        ),
        (
            "/system",
            (
                "CPU temperature",
                "Temperature warning threshold",
                "Load average (1 minute)",
                "Load average (5 minutes)",
                "Load average (15 minutes)",
                "Logical CPU count",
                "Memory use",
                "Memory warning threshold",
                "Root-storage use",
                "Storage warning threshold",
                "Host uptime",
                "Aurora dashboard service uptime",
                "Last observation time",
            ),
        ),
    ),
)
def test_detail_pages_render_requested_sanitized_fields(
    path: str,
    expected_fields: tuple[str, ...],
) -> None:
    page = render_portal(_report(), path, 5)
    assert "Current observation" in page
    assert "Last successful observation" in page
    assert "Request latency" in page
    assert "2.5 ms" in page
    assert "Not reported" not in page
    for expected in expected_fields:
        assert expected in page


def test_control_pages_explicitly_defer_controls_and_have_no_inputs() -> None:
    for path in ("/wled", "/hyperhdr"):
        page = render_portal(_report(), path, 5)
        assert "Controls planned for a future milestone" in page
        assert "Planned · Not active" in page
        assert "<button" not in page
        assert "<form" not in page
        assert "<input" not in page


@pytest.mark.parametrize("path", ("/room-map", "/spatial-intelligence"))
def test_future_feature_pages_are_unambiguously_inactive(path: str) -> None:
    page = render_portal(_report(), path, 5)
    assert "planned and not active" in page.lower()
    assert "Planned · Not active" in page
    assert "<button" not in page
    assert "<form" not in page


@pytest.mark.parametrize(
    "status",
    (HealthStatus.DEGRADED, HealthStatus.UNAVAILABLE),
)
def test_degraded_and_unavailable_components_remain_usable(
    status: HealthStatus,
) -> None:
    components = tuple(
        _component(
            name,
            status,
            message=f"Component is {status.value}",
            last_successful_at=None,
        )
        for name in ("wled", "hyperhdr", "capture", "raspberry_pi")
    )
    page = render_portal(_report(status, components=components), "/", 5)
    assert f'class="status-badge {status.value}"' in page
    assert f"Component is {status.value}" in page
    assert "No successful observation" in page
    assert "Not reported" in page


def test_portal_escapes_dynamic_values_and_ignores_unknown_details() -> None:
    wled = _component(
        "wled",
        message='<img src="x" alt="unsafe">',
        details={
            "firmware_version": "<script>unsafe()</script>",
            "configured_host": "SENSITIVE_HOST_CANARY",
            "raw_response": "SENSITIVE_RESPONSE_CANARY",
            "capture_path": "SENSITIVE_PATH_CANARY",
        },
    )
    report = _report(components=(wled,))
    page = render_portal(report, "/wled", 5)
    assert "&lt;img src=&quot;x&quot; alt=&quot;unsafe&quot;&gt;" in page
    assert "&lt;script&gt;unsafe()&lt;/script&gt;" in page
    assert "<script>unsafe()" not in page
    assert "SENSITIVE_HOST_CANARY" not in page
    assert "SENSITIVE_RESPONSE_CANARY" not in page
    assert "SENSITIVE_PATH_CANARY" not in page


def test_missing_components_and_optional_fields_render_safely() -> None:
    page = render_portal(_report(components=()), "/wled", 5)
    assert "No component observation is available" in page
    assert "Not reported" in page
    assert "No successful observation" in page
    assert "unavailable" in page


def test_api_health_remains_schema_version_one_and_shape_compatible() -> None:
    report = _report()
    service = StubHealthService(report)
    body, content_type, status = _request(service, "/api/health")
    payload = json.loads(body)
    assert status is HTTPStatus.OK
    assert content_type == "application/json; charset=utf-8"
    assert payload == json.loads(json.dumps(report.to_dict()))
    assert payload["schema_version"] == 1
    assert set(payload) == {
        "status",
        "checked_at",
        "service_uptime_seconds",
        "components",
        "schema_version",
    }


def test_navigation_reuses_single_flight_cache_without_extra_polls() -> None:
    calls = [0]

    def collect() -> ComponentHealth:
        calls[0] += 1
        return _component("wled")

    settings = load_settings(
        environment={},
        cli_overrides={"dashboard": {"refresh_seconds": 60}},
    )
    service = HealthService(settings, (CollectorSpec("wled", collect),))
    for path in sorted(PORTAL_PATHS):
        body, _, status = _request(service, path)
        assert status is HTTPStatus.OK
        assert body.startswith(b"<!doctype html>")
    assert calls == [1]


def test_local_stylesheet_is_served_without_collecting_health() -> None:
    service = StubHealthService(_report())
    body, content_type, status = _request(service, PORTAL_CSS_PATH)
    assert status is HTTPStatus.OK
    assert content_type == "text/css; charset=utf-8"
    assert b"@media (max-width:" in body
    assert b"https://" not in body
    assert service.calls == 0


@pytest.mark.parametrize("method", ("do_POST", "do_PUT", "do_PATCH", "do_DELETE"))
def test_mutation_methods_return_method_not_allowed_without_polling(
    method: str,
) -> None:
    service = StubHealthService(_report())
    handler = _handler(service)
    responses: list[tuple[bytes, str, HTTPStatus, dict[str, str]]] = []
    handler._send = (  # type: ignore[method-assign]
        lambda body, content_type, status=HTTPStatus.OK, headers=None: responses.append(
            (body, content_type, status, {} if headers is None else dict(headers))
        )
    )
    getattr(handler, method)()
    body, content_type, status, headers = responses[-1]
    assert body == b"method not allowed\n"
    assert content_type == "text/plain; charset=utf-8"
    assert status is HTTPStatus.METHOD_NOT_ALLOWED
    assert headers == {"Allow": "GET"}
    assert service.calls == 0


def test_responses_include_local_security_headers() -> None:
    handler = DashboardHandler.__new__(DashboardHandler)
    headers: dict[str, str] = {}
    handler.send_response = lambda status: None  # type: ignore[method-assign]
    handler.send_header = (  # type: ignore[method-assign]
        lambda name, value: headers.__setitem__(name, value)
    )
    handler.end_headers = lambda: None  # type: ignore[method-assign]
    handler.wfile = io.BytesIO()
    DashboardHandler._send(handler, b"portal", "text/html; charset=utf-8")
    assert headers["Cache-Control"] == "no-store"
    assert headers["Referrer-Policy"] == "no-referrer"
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["X-Frame-Options"] == "DENY"
    csp = headers["Content-Security-Policy"]
    assert "default-src 'none'" in csp
    assert "style-src 'self'" in csp
    assert "frame-ancestors 'none'" in csp
    assert "'unsafe-inline'" not in csp


def test_unknown_route_does_not_collect_health() -> None:
    service = StubHealthService(_report())
    body, _, status = _request(service, "/not-a-portal-route")
    assert status is HTTPStatus.NOT_FOUND
    assert body == b"not found\n"
    assert service.calls == 0
