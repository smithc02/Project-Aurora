"""Immutable sanitized models for explicit hardware validation."""

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


@dataclass(frozen=True, slots=True)
class HyperHDRServerInfo:
    server_info_received: bool
    hdr_mode_enabled: bool | None


@dataclass(frozen=True, slots=True)
class HyperHDRValidationReport:
    component_id: ComponentId
    state: ComponentHealthState
    reason_code: str
    message: str
    server_info_received: bool = False
    hdr_mode_enabled: bool | None = None


@dataclass(frozen=True, slots=True)
class CaptureDeviceProbeResult:
    reason_code: str
    device_node_present: bool = False
    character_device: bool = False
    v4l2_registered: bool = False
    process_read_access: bool = False
    device_name: str | None = None


@dataclass(frozen=True, slots=True)
class CaptureDeviceValidationReport:
    component_id: ComponentId
    state: ComponentHealthState
    reason_code: str
    message: str
    device_node_present: bool = False
    character_device: bool = False
    v4l2_registered: bool = False
    process_read_access: bool = False
    device_name: str | None = None
