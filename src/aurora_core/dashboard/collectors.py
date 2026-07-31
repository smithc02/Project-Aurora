"""Read-only health collectors for Project Aurora."""

from __future__ import annotations

import json
import os
import shutil
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from aurora_core.dashboard.models import ComponentHealth, HealthStatus, utc_now_iso
from aurora_core.dashboard.settings import DashboardSettings


def _component(name: str, status: HealthStatus, message: str, started: float, **details: Any) -> ComponentHealth:
    return ComponentHealth(
        name=name,
        status=status,
        message=message,
        checked_at=utc_now_iso(),
        latency_ms=round((time.monotonic() - started) * 1000, 2),
        details=details,
    )


def _json_request(url: str, timeout: float, payload: bytes | None = None) -> dict[str, Any]:
    headers = {"Accept": "application/json"}
    if payload is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=payload, headers=headers, method="POST" if payload is not None else "GET")
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - configured LAN endpoints only
        data = json.load(response)
    if not isinstance(data, dict):
        raise ValueError("response root is not an object")
    return data


def collect_wled(settings: DashboardSettings) -> ComponentHealth:
    started = time.monotonic()
    if not settings.wled_host:
        return _component("wled", HealthStatus.UNAVAILABLE, "WLED host is not configured", started)
    try:
        info = _json_request(f"http://{settings.wled_host}/json/info", settings.request_timeout_seconds)
        state = _json_request(f"http://{settings.wled_host}/json/state", settings.request_timeout_seconds)
    except (OSError, ValueError, urllib.error.URLError, json.JSONDecodeError) as exc:
        return _component("wled", HealthStatus.UNAVAILABLE, "WLED is unreachable", started, error=type(exc).__name__)

    leds = info.get("leds") if isinstance(info.get("leds"), dict) else {}
    reported_count = leds.get("count")
    count_matches = reported_count == settings.expected_led_count
    status = HealthStatus.HEALTHY if count_matches else HealthStatus.DEGRADED
    message = "WLED reachable and configuration matches" if count_matches else "WLED reachable but LED count differs from expected"
    return _component(
        "wled",
        status,
        message,
        started,
        firmware_version=info.get("ver"),
        uptime_seconds=info.get("uptime"),
        reported_led_count=reported_count,
        expected_led_count=settings.expected_led_count,
        active_leds=settings.expected_active_leds,
        skipped_leds=settings.expected_skipped_leds,
        brightness=state.get("bri"),
        output_on=state.get("on"),
    )


def collect_hyperhdr(settings: DashboardSettings) -> ComponentHealth:
    started = time.monotonic()
    payload = json.dumps({"command": "serverinfo", "tan": 1}).encode()
    try:
        data = _json_request(
            f"http://{settings.hyperhdr_host}:{settings.hyperhdr_port}/json-rpc",
            settings.request_timeout_seconds,
            payload,
        )
    except (OSError, ValueError, urllib.error.URLError, json.JSONDecodeError) as exc:
        return _component("hyperhdr", HealthStatus.UNAVAILABLE, "HyperHDR is unreachable", started, error=type(exc).__name__)

    success = data.get("success") is not False
    info = data.get("info") if isinstance(data.get("info"), dict) else {}
    components = info.get("components") if isinstance(info.get("components"), list) else []
    enabled = {
        str(item.get("name")): bool(item.get("enabled"))
        for item in components
        if isinstance(item, dict)
    }
    status = HealthStatus.HEALTHY if success else HealthStatus.DEGRADED
    return _component(
        "hyperhdr",
        status,
        "HyperHDR serverinfo response received" if success else "HyperHDR returned an unsuccessful response",
        started,
        components=enabled,
        grabber_active=enabled.get("VIDEOGRABBER") or enabled.get("GRABBER"),
        led_output_active=enabled.get("LEDDEVICE"),
    )


def collect_capture(settings: DashboardSettings) -> ComponentHealth:
    started = time.monotonic()
    path = Path(settings.capture_device)
    if not path.exists():
        return _component("capture", HealthStatus.UNAVAILABLE, "Capture device is missing", started, device=str(path))
    if not os.access(path, os.R_OK):
        return _component("capture", HealthStatus.DEGRADED, "Capture device exists but is not readable", started, device=str(path))
    name_path = Path("/sys/class/video4linux") / path.name / "name"
    device_name = name_path.read_text(encoding="utf-8").strip() if name_path.exists() else None
    return _component("capture", HealthStatus.HEALTHY, "Capture device is present and readable", started, device=str(path), device_name=device_name)


def _cpu_temperature() -> float | None:
    path = Path("/sys/class/thermal/thermal_zone0/temp")
    if not path.exists():
        return None
    return round(float(path.read_text(encoding="utf-8").strip()) / 1000, 1)


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
        return round(float(Path("/proc/uptime").read_text(encoding="utf-8").split()[0]), 1)
    except (OSError, ValueError, IndexError):
        return None


def collect_pi() -> ComponentHealth:
    started = time.monotonic()
    disk = shutil.disk_usage("/")
    disk_percent = round(disk.used / disk.total * 100, 1)
    temperature = _cpu_temperature()
    load_1, load_5, load_15 = os.getloadavg()
    memory_percent = _memory_percent()
    status = HealthStatus.HEALTHY
    warnings: list[str] = []
    if temperature is not None and temperature >= 80:
        status = HealthStatus.DEGRADED
        warnings.append("high CPU temperature")
    if disk_percent >= 90:
        status = HealthStatus.DEGRADED
        warnings.append("low root filesystem space")
    message = "Raspberry Pi health is within thresholds" if not warnings else "; ".join(warnings)
    return _component(
        "raspberry_pi",
        status,
        message,
        started,
        cpu_temperature_c=temperature,
        load_average_1m=round(load_1, 2),
        load_average_5m=round(load_5, 2),
        load_average_15m=round(load_15, 2),
        memory_used_percent=memory_percent,
        root_storage_used_percent=disk_percent,
        host_uptime_seconds=_host_uptime(),
    )
