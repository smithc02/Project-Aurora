"""Explicit hardware validation services."""

from aurora_core.hardware.capture_capability import validate_capture_capability
from aurora_core.hardware.capture_device import validate_capture_device
from aurora_core.hardware.capture_frame import validate_capture_frame
from aurora_core.hardware.capture_modes import validate_capture_modes
from aurora_core.hardware.ddp_output import validate_ddp_output
from aurora_core.hardware.hyperhdr import validate_hyperhdr
from aurora_core.hardware.models import (
    CaptureCapabilityProbeResult,
    CaptureCapabilityValidationReport,
    CaptureDeviceProbeResult,
    CaptureDeviceValidationReport,
    DDPOutputProbeResult,
    DDPOutputValidationReport,
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
    "validate_capture_modes",
    "validate_capture_frame",
    "DDPOutputProbeResult",
    "DDPOutputValidationReport",
    "validate_ddp_output",
]
