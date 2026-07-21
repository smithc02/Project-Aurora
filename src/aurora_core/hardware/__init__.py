"""Explicit hardware validation services."""

from aurora_core.hardware.models import WLEDDeviceInfo, WLEDValidationReport
from aurora_core.hardware.wled import expected_led_count, validate_wled

__all__ = [
    "WLEDDeviceInfo",
    "WLEDValidationReport",
    "expected_led_count",
    "validate_wled",
]
