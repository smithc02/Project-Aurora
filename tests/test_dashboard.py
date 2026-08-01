"""Tests for the read-only health dashboard."""

from __future__ import annotations

import io
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from http import HTTPStatus
from types import SimpleNamespace

import pytest

from aurora_core.config import load_settings
from aurora_core.dashboard.collectors import (
    CollectorSpec,
    collect_capture,
    collect_components,
    collect_hyperhdr,
    collect_pi,
    collect_wled,
)
from aurora_core.dashboard.models import (
    ComponentHealth,
    HealthReport,
    HealthStatus,
    overall_status,
)
from aurora_core.dashboard.server import DashboardHandler, _render_page
from aurora_core.dashboard.service import HealthService, collect_health
from aurora_core.hardware.errors import (
    HyperHDRTimeoutError,
    WLEDTimeoutError,
)
from aurora_core.hardware.models import CaptureDeviceProbeResult


class FakeWLEDInfoTransport:
    def __init__(self, result: bytes | Exception) -> None:
        self.result = result
        self.calls = 0

    def fetch_info(self, *, host: str, port: int, timeout_seconds: float) -> bytes:
        self.calls += 1
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class FakeWLEDStateTransport:
    def __init__(self, result: bytes | Exception) -> None:
        self.result = result
        self.calls = 0

    def fetch_state(self, *, host: str, port: int, timeout_seconds: float) -> bytes:
        self.calls += 1
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class FakeHyperHDRTransport:
    def __init__(self, result: bytes | Exception) -> None:
        self.result = result
        self.calls = 0

    def fetch_server_info(
        self, *, host: str, port: int, timeout_seconds: float
    ) -> bytes:
        self.calls += 1
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class FakeCaptureProbe:
    def __init__(self, result: CaptureDeviceProbeResult) -> None:
        self.result = result

    def probe(self, *, identifier: str) -> CaptureDeviceProbeResult:
        return self.result


def _settings(**overrides: object):
    base: dict[str, object] = {
        "wled": {
            "enabled": True,
            "host": "wled.invalid",
            "expected_led_count": 8,
            "expected_active_led_count": 6,
            "expected_skipped_leds": 2,
        },
        "hyperhdr": {
            "enabled": True,
            "host": "hyperhdr.invalid",
            "port": 8090,
        },
        "capture_device": {
            "enabled": True,
            "identifier": "/dev/video0",
        },
    }
    base.update(overrides)
    return load_settings(environment={}, cli_overrides=base)


def _component(
    name: str,
    status: HealthStatus,
    *,
    last_successful_at: str | None = None,
    details: dict[str, object] | None = None,
) -> ComponentHealth:
    return ComponentHealth(
        name=name,
        status=status,
        message="<safe test>",
        checked_at="2026-01-01T00:00:00+00:00",
        latency_ms=1.5,
        details={} if details is None else details,
        last_successful_at=last_successful_at,
    )


def test_overall_status_uses_worst_component_and_empty_is_unavailable() -> None:
    assert overall_status(()) is HealthStatus.UNAVAILABLE
    assert overall_status((_component("a", HealthStatus.HEALTHY),)) is (
        HealthStatus.HEALTHY
    )
    assert (
        overall_status(
            (
                _component("a", HealthStatus.HEALTHY),
                _component("b", HealthStatus.DEGRADED),
            )
        )
        is HealthStatus.DEGRADED
    )
    assert (
        overall_status(
            (
                _component("a", HealthStatus.HEALTHY),
                _component("b", HealthStatus.UNAVAILABLE),
            )
        )
        is HealthStatus.UNAVAILABLE
    )


def test_health_report_has_stable_json_serializable_shape() -> None:
    report = HealthReport(
        status=HealthStatus.HEALTHY,
        checked_at="2026-01-01T00:00:00+00:00",
        service_uptime_seconds=2.0,
        components=(_component("example", HealthStatus.HEALTHY),),
    ).to_dict()
    assert json.loads(json.dumps(report)) == {
        "status": "healthy",
        "checked_at": "2026-01-01T00:00:00+00:00",
        "service_uptime_seconds": 2.0,
        "components": [
            {
                "name": "example",
                "status": "healthy",
                "message": "<safe test>",
                "checked_at": "2026-01-01T00:00:00+00:00",
                "latency_ms": 1.5,
                "details": {},
                "last_successful_at": None,
            }
        ],
        "schema_version": 1,
    }


def test_page_renders_offline_component_and_escapes_text() -> None:
    report = HealthReport(
        status=HealthStatus.UNAVAILABLE,
        checked_at="2026-01-01T00:00:00+00:00",
        service_uptime_seconds=2.0,
        components=(
            _component(
                "wled",
                HealthStatus.UNAVAILABLE,
                details={
                    "firmware_version": "<script>",
                    "configured_host": "SHOULD_NOT_RENDER",
                },
            ),
        ),
    )
    page = _render_page(report, 5)
    assert "Project Aurora" in page
    assert "&lt;safe test&gt;" in page
    assert "&lt;script&gt;" in page
    assert "SHOULD_NOT_RENDER" not in page
    assert '<meta http-equiv="refresh" content="5">' in page
    assert "No successful observation" in page


def test_wled_healthy_observation_includes_sanitized_read_only_state() -> None:
    info = FakeWLEDInfoTransport(
        b'{"ver":"0.15.0","uptime":12,"leds":{"count":8,"pwr":120,"maxpwr":850}}'
    )
    state = FakeWLEDStateTransport(b'{"on":true,"bri":64}')
    result = collect_wled(_settings(), info, state)
    assert result.status is HealthStatus.HEALTHY
    assert result.last_successful_at == result.checked_at
    assert result.details == {
        "info_reason_code": "validated",
        "state_reason_code": "validated",
        "firmware_version": "0.15.0",
        "uptime_seconds": 12,
        "reported_led_count": 8,
        "expected_led_count": 8,
        "expected_active_led_count": 6,
        "expected_skipped_leds": 2,
        "led_count_matches": True,
        "estimated_current_milliamps": 120,
        "current_limit_milliamps": 850,
        "brightness": 64,
        "output_on": True,
    }
    assert info.calls == state.calls == 1
    assert "wled.invalid" not in repr(result)


def test_wled_degraded_mismatch_malformed_and_partial_failure() -> None:
    mismatch = collect_wled(
        _settings(),
        FakeWLEDInfoTransport(b'{"ver":"0.15.0","leds":{"count":7}}'),
        FakeWLEDStateTransport(b'{"on":true,"bri":64}'),
    )
    assert mismatch.status is HealthStatus.DEGRADED
    assert mismatch.details["led_count_matches"] is False

    malformed = collect_wled(
        _settings(),
        FakeWLEDInfoTransport(b"{"),
        FakeWLEDStateTransport(b'{"on":true,"bri":64}'),
    )
    assert malformed.status is HealthStatus.DEGRADED
    assert malformed.details["info_reason_code"] == "invalid_json"
    assert malformed.last_successful_at is not None

    partial = collect_wled(
        _settings(),
        FakeWLEDInfoTransport(b'{"ver":"0.15.0","leds":{"count":8}}'),
        FakeWLEDStateTransport(WLEDTimeoutError()),
    )
    assert partial.status is HealthStatus.DEGRADED
    assert partial.details["state_reason_code"] == "timeout"


def test_wled_timeout_and_disabled_are_unavailable() -> None:
    timeout = collect_wled(
        _settings(),
        FakeWLEDInfoTransport(WLEDTimeoutError()),
        FakeWLEDStateTransport(WLEDTimeoutError()),
    )
    assert timeout.status is HealthStatus.UNAVAILABLE
    assert timeout.last_successful_at is None

    disabled_settings = load_settings(environment={})
    info = FakeWLEDInfoTransport(b"{}")
    state = FakeWLEDStateTransport(b"{}")
    disabled = collect_wled(disabled_settings, info, state)
    assert disabled.status is HealthStatus.UNAVAILABLE
    assert info.calls == state.calls == 0


def test_wled_state_production_transport_is_fixed_get(monkeypatch) -> None:
    from aurora_core.hardware.transport import UrllibWLEDStateTransport

    captured: dict[str, object] = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def getcode(self) -> int:
            return 200

        def read(self, amount: int) -> bytes:
            captured["amount"] = amount
            return b'{"on":true,"bri":1}'

    class Opener:
        def open(self, request: object, timeout: float) -> Response:
            captured["request"] = request
            captured["timeout"] = timeout
            return Response()

    monkeypatch.setattr(
        "aurora_core.hardware.transport.build_opener",
        lambda handler: Opener(),
    )
    body = UrllibWLEDStateTransport().fetch_state(
        host="wled.invalid",
        port=80,
        timeout_seconds=1.5,
    )
    request = captured["request"]
    assert body.startswith(b"{")
    assert request.get_method() == "GET"  # type: ignore[union-attr]
    assert request.data is None  # type: ignore[union-attr]
    assert request.full_url == "http://wled.invalid:80/json/state"  # type: ignore[union-attr]


def test_hyperhdr_healthy_degraded_timeout_and_malformed() -> None:
    healthy = collect_hyperhdr(
        _settings(),
        FakeHyperHDRTransport(
            b'{"success":true,"info":{"instance":[{"running":true}],'
            b'"components":[{"name":"VIDEOGRABBER","enabled":true},'
            b'{"name":"LEDDEVICE","enabled":true}]}}'
        ),
    )
    assert healthy.status is HealthStatus.HEALTHY
    assert healthy.details["instance_running"] is True
    assert healthy.details["grabber_active"] is True
    assert healthy.details["led_output_active"] is True

    degraded = collect_hyperhdr(
        _settings(),
        FakeHyperHDRTransport(
            b'{"success":true,"info":{"components":'
            b'[{"name":"VIDEOGRABBER","enabled":false}]}}'
        ),
    )
    assert degraded.status is HealthStatus.DEGRADED
    assert "grabber" in degraded.message

    timeout = collect_hyperhdr(
        _settings(),
        FakeHyperHDRTransport(HyperHDRTimeoutError()),
    )
    assert timeout.status is HealthStatus.UNAVAILABLE
    assert timeout.details["reason_code"] == "timeout"

    malformed = collect_hyperhdr(
        _settings(),
        FakeHyperHDRTransport(b"{"),
    )
    assert malformed.status is HealthStatus.DEGRADED
    assert malformed.details["reason_code"] == "invalid_json"


@pytest.mark.parametrize(
    ("probe_result", "expected"),
    [
        (
            CaptureDeviceProbeResult(
                "validated",
                True,
                True,
                True,
                True,
                "Generic UVC capture",
            ),
            HealthStatus.HEALTHY,
        ),
        (
            CaptureDeviceProbeResult("device_not_found"),
            HealthStatus.UNAVAILABLE,
        ),
        (
            CaptureDeviceProbeResult(
                "permission_denied",
                True,
                True,
                True,
                False,
                "Generic UVC capture",
            ),
            HealthStatus.DEGRADED,
        ),
    ],
)
def test_capture_health_states(
    probe_result: CaptureDeviceProbeResult,
    expected: HealthStatus,
) -> None:
    result = collect_capture(
        _settings(),
        FakeCaptureProbe(probe_result),
        platform="linux",
    )
    assert result.status is expected
    assert "/dev/video0" not in repr(result)


def test_pi_metrics_are_partial_failure_tolerant(monkeypatch) -> None:
    settings = _settings()
    monkeypatch.setattr(
        "aurora_core.dashboard.collectors._cpu_temperature",
        lambda: 55.0,
    )
    monkeypatch.setattr(
        "aurora_core.dashboard.collectors._memory_percent",
        lambda: 40.0,
    )
    monkeypatch.setattr(
        "aurora_core.dashboard.collectors._host_uptime",
        lambda: 100.0,
    )
    monkeypatch.setattr(
        "aurora_core.dashboard.collectors._disk_percent",
        lambda: 30.0,
    )
    monkeypatch.setattr(
        "aurora_core.dashboard.collectors._load_averages",
        lambda: (0.1, 0.2, 0.3),
    )
    assert collect_pi(settings).status is HealthStatus.HEALTHY

    monkeypatch.setattr(
        "aurora_core.dashboard.collectors._cpu_temperature",
        lambda: 81.0,
    )
    monkeypatch.setattr(
        "aurora_core.dashboard.collectors._memory_percent",
        lambda: None,
    )
    degraded = collect_pi(settings)
    assert degraded.status is HealthStatus.DEGRADED
    assert "high CPU temperature" in degraded.message
    assert "memory utilization" in degraded.message

    for name in (
        "_cpu_temperature",
        "_memory_percent",
        "_host_uptime",
        "_disk_percent",
        "_load_averages",
    ):
        monkeypatch.setattr(
            f"aurora_core.dashboard.collectors.{name}",
            lambda: None,
        )
    assert collect_pi(settings).status is HealthStatus.UNAVAILABLE


def test_collectors_run_concurrently_and_fail_independently() -> None:
    barrier = threading.Barrier(2)

    def synchronized(name: str, status: HealthStatus) -> ComponentHealth:
        barrier.wait(timeout=1)
        return _component(name, status)

    def fail() -> ComponentHealth:
        raise RuntimeError("must not escape")

    components = collect_components(
        _settings(),
        (
            CollectorSpec(
                "healthy",
                lambda: synchronized("healthy", HealthStatus.HEALTHY),
            ),
            CollectorSpec(
                "degraded",
                lambda: synchronized("degraded", HealthStatus.DEGRADED),
            ),
            CollectorSpec("offline", fail),
        ),
    )
    assert tuple(component.name for component in components) == (
        "healthy",
        "degraded",
        "offline",
    )
    assert components[2].status is HealthStatus.UNAVAILABLE
    report = collect_health(
        _settings(),
        tuple(
            CollectorSpec(component.name, lambda c=component: c)
            for component in components
        ),
    )
    assert report.status is HealthStatus.UNAVAILABLE


def test_capture_activity_is_inferred_without_opening_or_reconfiguring() -> None:
    capture = _component("capture", HealthStatus.HEALTHY)
    active_hyperhdr = _component(
        "hyperhdr",
        HealthStatus.HEALTHY,
        details={"grabber_active": True},
    )
    components = collect_components(
        _settings(),
        (
            CollectorSpec("capture", lambda: capture),
            CollectorSpec("hyperhdr", lambda: active_hyperhdr),
        ),
    )
    assert components[0].status is HealthStatus.HEALTHY
    assert components[0].details["activity_source"] == "HyperHDR serverinfo"

    inactive_hyperhdr = _component(
        "hyperhdr",
        HealthStatus.DEGRADED,
        details={"grabber_active": False},
    )
    components = collect_components(
        _settings(),
        (
            CollectorSpec("capture", lambda: capture),
            CollectorSpec("hyperhdr", lambda: inactive_hyperhdr),
        ),
    )
    assert components[0].status is HealthStatus.DEGRADED


def test_health_service_caches_single_flight_and_retains_last_success() -> None:
    now = [0.0]
    calls = [0]
    release = threading.Event()
    started = threading.Event()

    def collect() -> ComponentHealth:
        calls[0] += 1
        started.set()
        release.wait(timeout=1)
        if calls[0] == 1:
            return _component(
                "wled",
                HealthStatus.HEALTHY,
                last_successful_at="2026-01-01T00:00:00+00:00",
            )
        return _component("wled", HealthStatus.UNAVAILABLE)

    service = HealthService(
        _settings(dashboard={"refresh_seconds": 5}),
        (CollectorSpec("wled", collect),),
        clock=lambda: now[0],
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(service.get_health)
        assert started.wait(timeout=1)
        second = pool.submit(service.get_health)
        release.set()
        first_report = first.result(timeout=1)
        second_report = second.result(timeout=1)
    assert first_report is second_report
    assert calls[0] == 1

    now[0] = 5.0
    offline = service.get_health()
    assert calls[0] == 2
    assert offline.components[0].status is HealthStatus.UNAVAILABLE
    assert offline.components[0].last_successful_at == "2026-01-01T00:00:00+00:00"


def test_http_api_and_page_remain_available_during_partial_failure() -> None:
    service = HealthService(
        _settings(),
        (
            CollectorSpec(
                "wled",
                lambda: _component("wled", HealthStatus.HEALTHY),
            ),
            CollectorSpec(
                "hyperhdr",
                lambda: _component("hyperhdr", HealthStatus.UNAVAILABLE),
            ),
        ),
    )
    handler = DashboardHandler.__new__(DashboardHandler)
    handler.server = SimpleNamespace(health_service=service, refresh_seconds=5)
    responses: list[tuple[bytes, str, HTTPStatus]] = []
    handler._send = lambda body, content_type, status=HTTPStatus.OK: responses.append(  # type: ignore[method-assign]
        (body, content_type, status)
    )

    handler.path = "/api/health"
    handler.do_GET()
    payload = json.loads(responses[-1][0])
    assert responses[-1][1] == "application/json; charset=utf-8"
    assert responses[-1][2] == HTTPStatus.OK
    assert payload["status"] == "unavailable"
    assert payload["components"][1]["status"] == "unavailable"

    handler.path = "/"
    handler.do_GET()
    page = responses[-1][0].decode()
    assert responses[-1][1] == "text/html; charset=utf-8"
    assert "HyperHDR" in page
    assert "unavailable" in page

    headers: dict[str, str] = {}
    handler.send_response = lambda status: None  # type: ignore[method-assign]
    handler.send_header = (  # type: ignore[method-assign]
        lambda key, value: headers.__setitem__(key, value)
    )
    handler.end_headers = lambda: None  # type: ignore[method-assign]
    handler.wfile = io.BytesIO()
    DashboardHandler._send(handler, b"{}", "application/json; charset=utf-8")
    assert headers["Cache-Control"] == "no-store"
    assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]
