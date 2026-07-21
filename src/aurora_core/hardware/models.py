"""Immutable sanitized WLED validation models."""

from __future__ import annotations

from dataclasses import dataclass

from aurora_core.runtime.models import ComponentHealthState, ComponentId


@dataclass(frozen=True, slots=True)
class WLEDDeviceInfo:
    firmware_version: str
    led_count: int


@dataclass(frozen=True, slots=True)
class WLEDValidationReport:
    component_id: ComponentId
    state: ComponentHealthState
    reason_code: str
    message: str
    firmware_version: str | None = None
    reported_led_count: int | None = None
    expected_led_count: int | None = None
    led_count_matches: bool | None = None
