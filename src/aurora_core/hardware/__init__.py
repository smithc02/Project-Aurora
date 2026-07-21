"""Explicit hardware validation services."""

from aurora_core.hardware.capture_capability import validate_capture_capability
from aurora_core.hardware.capture_device import validate_capture_device
from aurora_core.hardware.hyperhdr import validate_hyperhdr
from aurora_core.hardware.models import (
    CaptureCapabilityProbeResult,
    CaptureCapabilityValidationReport,
    CaptureDeviceProbeResult,
    CaptureDeviceValidationReport,
    HyperHDRServerInfo,
    HyperHDRValidationReport,
    WLEDDeviceInfo,
    WLEDValidationReport,
)
from aurora_core.hardware.wled import expected_led_count, validate_wled

__all__ = [
    "WLEDDeviceInfo",
    "WLEDValidationReport",
    "HyperHDRServerInfo",
    "HyperHDRValidationReport",
    "expected_led_count",
    "validate_hyperhdr",
    "validate_wled",
    "CaptureDeviceProbeResult",
    "CaptureDeviceValidationReport",
    "validate_capture_device",
    "CaptureCapabilityProbeResult",
    "CaptureCapabilityValidationReport",
    "validate_capture_capability",
]
