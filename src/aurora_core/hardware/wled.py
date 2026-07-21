"""One-shot, read-only WLED /json/info validation."""

from __future__ import annotations

import json
from collections.abc import Mapping

from aurora_core.config.models import AuroraSettings, LightingZoneSettings
from aurora_core.hardware.errors import WLEDTransportError
from aurora_core.hardware.models import WLEDDeviceInfo, WLEDValidationReport
from aurora_core.hardware.transport import UrllibWLEDInfoTransport, WLEDInfoTransport
from aurora_core.runtime.models import ComponentHealthState, ComponentId


def expected_led_count(zones: tuple[LightingZoneSettings, ...]) -> int | None:
    enabled = tuple(zone for zone in zones if zone.enabled)
    if not enabled or any(zone.led_count is None for zone in enabled):
        return None
    return sum(zone.led_count for zone in enabled if zone.led_count is not None)


def parse_wled_info(body: bytes) -> WLEDDeviceInfo:
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("invalid_json") from error
    if not isinstance(payload, Mapping):
        raise ValueError("invalid_response")
    version, leds = payload.get("ver"), payload.get("leds")
    if not isinstance(version, str) or not version or not isinstance(leds, Mapping):
        raise ValueError("invalid_response")
    count = leds.get("count")
    if isinstance(count, bool) or not isinstance(count, int) or count <= 0:
        raise ValueError("invalid_response")
    return WLEDDeviceInfo(version, count)


def validate_wled(
    settings: AuroraSettings,
    transport: WLEDInfoTransport | None = None,
    *,
    timeout_seconds: float | None = None,
) -> WLEDValidationReport:
    if not settings.wled.enabled:
        return WLEDValidationReport(
            ComponentId.WLED,
            ComponentHealthState.DISABLED,
            "wled_disabled",
            "WLED is disabled.",
        )
    active_transport = UrllibWLEDInfoTransport() if transport is None else transport
    try:
        body = active_transport.fetch_info(
            host=settings.wled.host or "",
            port=settings.wled.port or 80,
            timeout_seconds=settings.wled.validation_timeout_seconds
            if timeout_seconds is None
            else timeout_seconds,
        )
        info = parse_wled_info(body)
    except WLEDTransportError as error:
        return WLEDValidationReport(
            ComponentId.WLED,
            ComponentHealthState.UNHEALTHY,
            error.reason_code,
            "Read-only WLED validation failed.",
        )
    except ValueError as error:
        reason = str(error)
        return WLEDValidationReport(
            ComponentId.WLED,
            ComponentHealthState.UNHEALTHY,
            reason,
            "WLED returned an invalid information response.",
        )
    expected = expected_led_count(settings.lighting_zones)
    matches = None if expected is None else info.led_count == expected
    if matches is False:
        return WLEDValidationReport(
            ComponentId.WLED,
            ComponentHealthState.DEGRADED,
            "led_count_mismatch",
            "WLED LED count does not match enabled zones.",
            info.firmware_version,
            info.led_count,
            expected,
            False,
        )
    return WLEDValidationReport(
        ComponentId.WLED,
        ComponentHealthState.HEALTHY,
        "validated",
        "Read-only WLED validation succeeded.",
        info.firmware_version,
        info.led_count,
        expected,
        matches,
    )
