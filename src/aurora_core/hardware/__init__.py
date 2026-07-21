"""Explicit hardware validation services."""

from aurora_core.hardware.hyperhdr import validate_hyperhdr
from aurora_core.hardware.models import (
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
]
