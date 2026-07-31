"""Bounded, read-only health collectors for Project Aurora."""

from __future__ import annotations

import os
import shutil
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from pathlib import Path

from aurora_core.config.models import AuroraSettings
from aurora_core.dashboard.models import ComponentHealth, HealthStatus, utc_now_iso
from aurora_core.hardware.capture_device import validate_capture_device
from aurora_core.hardware.capture_probe import CaptureDeviceProbe
from aurora_core.hardware.errors import WLEDTransportError
from aurora_core.hardware.hyperhdr import validate_hyperhdr
from aurora_core.hardware.hyperhdr_transport import HyperHDRServerInfoTransport
from aurora_core.hardware.models import WLEDState
from aurora_core.hardware.transport import (
    UrllibWLEDStateTransport,
    WLEDInfoTransport,
    WLEDStateTransport,
)
from aurora_core.hardware.wled import parse_wled_state, validate_wled
from aurora_core.runtime.models import ComponentHealthState

Collector = Callable[[], ComponentHealth]
_UNAVAILABLE_NETWORK_REASONS = frozenset({"connection_failed", "timeout"})
_UNAVAILABLE_CAPTURE_REASONS = frozenset(
    {
        "capture_device_disabled",
        "device_not_found",
        "probe_failed",
        "symlink_resolution_failed",
        "unsupported_platform",
    }
)


@dataclass(frozen=True, slots=True)
class CollectorSpec:
    """A named collector used for deterministic, isolated aggregation."""

    name: str
    collect: Collector


def _component(
    name: str,
    status: HealthStatus,
    message: str,
    started: float,
    *,
    observation_succeeded: bool = False,
    details: dict[str, object] | None = None,
) -> ComponentHealth:
    checked_at = utc_now_iso()
    return ComponentHealth(
        name=name,
        status=status,
        message=message,
        checked_at=checked_at,
        latency_ms=round((time.monotonic() - started) * 1000, 2),
        details={} if details is None else details,
        last_successful_at=checked_at if observation_succeeded else None,
    )


def _network_failure_status(reason_code: str) -> HealthStatus:
    if reason_code in _UNAVAILABLE_NETWORK_REASONS:
        return HealthStatus.UNAVAILABLE
    return HealthStatus.DEGRADED


def _fetch_wled_state(
    settings: AuroraSettings,
    transport: WLEDStateTransport,
) -> tuple[WLEDState | None, str]:
    try:
        body = transport.fetch_state(
            host=settings.wled.host or "",
            port=settings.wled.port or 80,
            timeout_seconds=settings.wled.validation_timeout_seconds,
        )
        return parse_wled_state(body), "validated"
    except WLEDTransportError as error:
        return None, error.reason_code
    except ValueError as error:
        return None, str(error)


def collect_wled(
    settings: AuroraSettings,
    info_transport: WLEDInfoTransport | None = None,
    state_transport: WLEDStateTransport | None = None,
) -> ComponentHealth:
    """Observe WLED info and state through two fixed GET-only transports."""
    started = time.monotonic()
    if not settings.wled.enabled:
        return _component(
            "wled",
            HealthStatus.UNAVAILABLE,
            "WLED is not enabled in Aurora configuration",
            started,
            details={"reason_code": "wled_disabled"},
        )

    active_state_transport = (
        UrllibWLEDStateTransport() if state_transport is None else state_transport
    )
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="aurora-wled") as pool:
        info_future = pool.submit(validate_wled, settings, info_transport)
        state_future = pool.submit(
            _fetch_wled_state,
            settings,
            active_state_transport,
        )
        info = info_future.result()
        state, state_reason = state_future.result()

    if info.state is ComponentHealthState.HEALTHY:
        info_status = HealthStatus.HEALTHY
    elif info.state is ComponentHealthState.DEGRADED:
        info_status = HealthStatus.DEGRADED
    else:
        info_status = _network_failure_status(info.reason_code)
    state_status = (
        HealthStatus.HEALTHY
        if state is not None
        else _network_failure_status(state_reason)
    )

    successful_observations = (
        info.state in {ComponentHealthState.HEALTHY, ComponentHealthState.DEGRADED},
        state is not None,
    )
    if info_status is HealthStatus.HEALTHY and state_status is HealthStatus.HEALTHY:
        status = HealthStatus.HEALTHY
        message = "WLED read-only health checks succeeded"
    elif (
        info_status is HealthStatus.UNAVAILABLE
        and state_status is HealthStatus.UNAVAILABLE
    ):
        status = HealthStatus.UNAVAILABLE
        message = "WLED could not be observed"
    else:
        status = HealthStatus.DEGRADED
        message = "WLED was only partially observed or differs from expectations"

    details: dict[str, object] = {
        "info_reason_code": info.reason_code,
        "state_reason_code": state_reason,
        "firmware_version": info.firmware_version,
        "uptime_seconds": info.uptime_seconds,
        "reported_led_count": info.reported_led_count,
        "expected_led_count": info.expected_led_count,
        "expected_active_led_count": settings.wled.expected_active_led_count,
        "expected_skipped_leds": settings.wled.expected_skipped_leds,
        "led_count_matches": info.led_count_matches,
        "estimated_current_milliamps": info.current_milliamps,
        "current_limit_milliamps": info.current_limit_milliamps,
        "brightness": None if state is None else state.brightness,
        "output_on": None if state is None else state.output_on,
    }
    return _component(
        "wled",
        status,
        message,
        started,
        observation_succeeded=any(successful_observations),
        details=details,
    )


def collect_hyperhdr(
    settings: AuroraSettings,
    transport: HyperHDRServerInfoTransport | None = None,
) -> ComponentHealth:
    """Observe HyperHDR through the existing fixed GET serverinfo validator."""
    started = time.monotonic()
    report = validate_hyperhdr(settings, transport)
    details: dict[str, object] = {
        "reason_code": report.reason_code,
        "server_info_received": report.server_info_received,
        "hdr_mode_enabled": report.hdr_mode_enabled,
        "instance_running": report.instance_running,
        "grabber_active": report.grabber_active,
        "led_output_active": report.led_output_active,
    }
    if report.state is ComponentHealthState.DISABLED:
        return _component(
            "hyperhdr",
            HealthStatus.UNAVAILABLE,
            "HyperHDR is not enabled in Aurora configuration",
            started,
            details=details,
        )
    if report.state is ComponentHealthState.UNHEALTHY:
        return _component(
            "hyperhdr",
            _network_failure_status(report.reason_code),
            report.message,
            started,
            details=details,
        )

    inactive = tuple(
        name
        for name, value in (
            ("instance", report.instance_running),
            ("grabber", report.grabber_active),
            ("LED output", report.led_output_active),
        )
        if value is False
    )
    if inactive:
        return _component(
            "hyperhdr",
            HealthStatus.DEGRADED,
            f"HyperHDR is reachable but inactive: {', '.join(inactive)}",
            started,
            observation_succeeded=True,
            details=details,
        )
    return _component(
        "hyperhdr",
        HealthStatus.HEALTHY,
        "HyperHDR read-only serverinfo check succeeded",
        started,
        observation_succeeded=True,
        details=details,
    )


def collect_capture(
    settings: AuroraSettings,
    probe: CaptureDeviceProbe | None = None,
    *,
    platform: str | None = None,
) -> ComponentHealth:
    """Reuse the non-opening V4L2 metadata validator."""
    started = time.monotonic()
    report = validate_capture_device(settings, probe, platform=platform)
    details: dict[str, object] = {
        "reason_code": report.reason_code,
        "device_node_present": report.device_node_present,
        "character_device": report.character_device,
        "v4l2_registered": report.v4l2_registered,
        "process_read_access": report.process_read_access,
        "device_name": report.device_name,
    }
    if report.state is ComponentHealthState.HEALTHY:
        return _component(
            "capture",
            HealthStatus.HEALTHY,
            "Capture device metadata validation succeeded",
            started,
            observation_succeeded=True,
            details=details,
        )
    status = (
        HealthStatus.UNAVAILABLE
        if report.reason_code in _UNAVAILABLE_CAPTURE_REASONS
        else HealthStatus.DEGRADED
    )
    return _component(
        "capture",
        status,
        report.message,
        started,
        observation_succeeded=report.device_node_present,
        details=details,
    )


def _cpu_temperature() -> float | None:
    try:
        raw = Path("/sys/class/thermal/thermal_zone0/temp").read_text(encoding="utf-8")
        return round(float(raw.strip()) / 1000, 1)
    except (OSError, ValueError):
        return None


def _memory_percent() -> float | None:
    values: dict[str, int] = {}
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            key, raw = line.split(":", 1)
            values[key] = int(raw.strip().split()[0])
        total, available = values["MemTotal"], values["MemAvailable"]
        return round((total - available) / total * 100, 1)
    except (OSError, KeyError, ValueError, ZeroDivisionError):
        return None


def _host_uptime() -> float | None:
    try:
        raw = Path("/proc/uptime").read_text(encoding="utf-8")
        return round(float(raw.split()[0]), 1)
    except (OSError, ValueError, IndexError):
        return None


def _disk_percent() -> float | None:
    try:
        disk = shutil.disk_usage("/")
        return round(disk.used / disk.total * 100, 1)
    except (OSError, ZeroDivisionError):
        return None


def _load_averages() -> tuple[float, float, float] | None:
    try:
        return os.getloadavg()
    except OSError:
        return None


def collect_pi(settings: AuroraSettings) -> ComponentHealth:
    """Collect metrics independently so one missing source does not abort the rest."""
    started = time.monotonic()
    temperature = _cpu_temperature()
    memory_percent = _memory_percent()
    host_uptime = _host_uptime()
    disk_percent = _disk_percent()
    load = _load_averages()

    warnings: list[str] = []
    missing: list[str] = []
    for name, value in (
        ("CPU temperature", temperature),
        ("memory utilization", memory_percent),
        ("host uptime", host_uptime),
        ("root filesystem utilization", disk_percent),
        ("load averages", load),
    ):
        if value is None:
            missing.append(name)
    if (
        temperature is not None
        and temperature >= settings.dashboard.cpu_temperature_warning_c
    ):
        warnings.append("high CPU temperature")
    if (
        memory_percent is not None
        and memory_percent >= settings.dashboard.memory_warning_percent
    ):
        warnings.append("high memory utilization")
    if (
        disk_percent is not None
        and disk_percent >= settings.dashboard.storage_warning_percent
    ):
        warnings.append("low root filesystem space")

    observations = (temperature, memory_percent, host_uptime, disk_percent, load)
    if all(value is None for value in observations):
        status = HealthStatus.UNAVAILABLE
        message = "Raspberry Pi metrics are unavailable"
    elif warnings or missing:
        status = HealthStatus.DEGRADED
        messages = [*warnings]
        if missing:
            messages.append(f"unavailable metrics: {', '.join(missing)}")
        message = "; ".join(messages)
    else:
        status = HealthStatus.HEALTHY
        message = "Raspberry Pi health is within configured thresholds"

    details: dict[str, object] = {
        "cpu_temperature_c": temperature,
        "cpu_temperature_warning_c": (settings.dashboard.cpu_temperature_warning_c),
        "load_average_1m": None if load is None else round(load[0], 2),
        "load_average_5m": None if load is None else round(load[1], 2),
        "load_average_15m": None if load is None else round(load[2], 2),
        "logical_cpu_count": os.cpu_count(),
        "memory_used_percent": memory_percent,
        "memory_warning_percent": settings.dashboard.memory_warning_percent,
        "root_storage_used_percent": disk_percent,
        "storage_warning_percent": settings.dashboard.storage_warning_percent,
        "host_uptime_seconds": host_uptime,
    }
    return _component(
        "raspberry_pi",
        status,
        message,
        started,
        observation_succeeded=not all(value is None for value in observations),
        details=details,
    )


def default_collectors(settings: AuroraSettings) -> tuple[CollectorSpec, ...]:
    """Build the fixed set of read-only collectors."""
    return (
        CollectorSpec("wled", lambda: collect_wled(settings)),
        CollectorSpec("hyperhdr", lambda: collect_hyperhdr(settings)),
        CollectorSpec("capture", lambda: collect_capture(settings)),
        CollectorSpec("raspberry_pi", lambda: collect_pi(settings)),
    )


def collect_components(
    settings: AuroraSettings,
    collectors: tuple[CollectorSpec, ...] | None = None,
) -> tuple[ComponentHealth, ...]:
    """Run isolated collectors concurrently and retain deterministic ordering."""
    active_collectors = (
        default_collectors(settings) if collectors is None else collectors
    )
    if not active_collectors:
        return ()
    with ThreadPoolExecutor(
        max_workers=len(active_collectors),
        thread_name_prefix="aurora-health",
    ) as pool:
        futures = tuple(
            pool.submit(_safe_collect, spec.name, spec.collect)
            for spec in active_collectors
        )
        components = tuple(future.result() for future in futures)
    return _apply_capture_activity(components)


def _safe_collect(name: str, collector: Collector) -> ComponentHealth:
    started = time.monotonic()
    try:
        return collector()
    except Exception:
        return _component(
            name,
            HealthStatus.UNAVAILABLE,
            "Health collector failed safely",
            started,
            details={"reason_code": "collector_failed"},
        )


def _apply_capture_activity(
    components: tuple[ComponentHealth, ...],
) -> tuple[ComponentHealth, ...]:
    by_name = {component.name: component for component in components}
    capture = by_name.get("capture")
    hyperhdr = by_name.get("hyperhdr")
    if (
        capture is None
        or hyperhdr is None
        or capture.status is HealthStatus.UNAVAILABLE
    ):
        return components

    grabber_active = hyperhdr.details.get("grabber_active")
    details = {
        **capture.details,
        "activity_source": "HyperHDR serverinfo",
        "grabber_active": grabber_active,
    }
    if grabber_active is True:
        updated = replace(
            capture,
            message=(
                "Capture device is available and HyperHDR reports an active grabber"
            ),
            details=details,
        )
    elif grabber_active is False:
        updated = replace(
            capture,
            status=HealthStatus.DEGRADED,
            message=(
                "Capture device is available but HyperHDR reports an inactive grabber"
            ),
            details=details,
        )
    else:
        updated = replace(
            capture,
            status=HealthStatus.DEGRADED,
            message="Capture device is available but current activity is not reported",
            details=details,
        )
    return tuple(
        updated if component.name == "capture" else component
        for component in components
    )
