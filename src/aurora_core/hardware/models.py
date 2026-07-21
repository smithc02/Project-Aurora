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


@dataclass(frozen=True, slots=True)
class CaptureCapabilityProbeResult:
    """Sanitized result of the fixed V4L2 capability query."""

    reason_code: str
    query_succeeded: bool = False
    driver_name: str | None = None
    card_name: str | None = None
    v4l2_api_version: str | None = None
    single_planar_capture: bool = False
    multi_planar_capture: bool = False
    streaming_io: bool = False
    readwrite_io: bool = False
    device_was_opened: bool = False
    ioctl_was_issued: bool = False
    descriptor_was_closed: bool = False


@dataclass(frozen=True, slots=True)
class CaptureCapabilityValidationReport:
    """Path-free public report for the query-only V4L2 capability boundary."""

    component_id: ComponentId
    state: ComponentHealthState
    reason_code: str
    message: str
    query_succeeded: bool = False
    driver_name: str | None = None
    card_name: str | None = None
    v4l2_api_version: str | None = None
    single_planar_capture: bool = False
    multi_planar_capture: bool = False
    streaming_io: bool = False
    readwrite_io: bool = False
    device_was_opened: bool = False
    ioctl_was_issued: bool = False
    descriptor_was_closed: bool = False


@dataclass(frozen=True, slots=True)
class CaptureFrameInterval:
    kind: str
    numerator: int | None = None
    denominator: int | None = None
    min_numerator: int | None = None
    min_denominator: int | None = None
    max_numerator: int | None = None
    max_denominator: int | None = None
    step_numerator: int | None = None
    step_denominator: int | None = None


@dataclass(frozen=True, slots=True)
class CaptureFrameSize:
    kind: str
    width: int | None = None
    height: int | None = None
    min_width: int | None = None
    max_width: int | None = None
    step_width: int | None = None
    min_height: int | None = None
    max_height: int | None = None
    step_height: int | None = None
    intervals: tuple[CaptureFrameInterval, ...] = ()
    intervals_enumerated: bool = False


@dataclass(frozen=True, slots=True)
class CapturePixelFormat:
    queue_type: str
    fourcc: str
    big_endian: bool
    description: str | None
    compressed: bool
    emulated: bool
    frame_sizes: tuple[CaptureFrameSize, ...] = ()


@dataclass(frozen=True, slots=True)
class CaptureModeProbeResult:
    reason_code: str
    formats: tuple[CapturePixelFormat, ...] = ()
    enumeration_complete: bool = False
    partial_reason_codes: tuple[str, ...] = ()
    device_was_opened: bool = False
    querycap_was_issued: bool = False
    enumeration_ioctl_was_issued: bool = False
    descriptor_was_closed: bool = False


@dataclass(frozen=True, slots=True)
class CaptureModeValidationReport:
    component_id: ComponentId
    state: ComponentHealthState
    reason_code: str
    message: str
    formats: tuple[CapturePixelFormat, ...] = ()
    format_count: int = 0
    frame_size_count: int = 0
    frame_interval_count: int = 0
    enumeration_complete: bool = False
    partial_reason_codes: tuple[str, ...] = ()
    device_was_opened: bool = False
    querycap_was_issued: bool = False
    enumeration_ioctl_was_issued: bool = False
    descriptor_was_closed: bool = False
