"""One-shot capture-device validation service."""

from __future__ import annotations

import sys

from aurora_core.config.models import AuroraSettings
from aurora_core.hardware.capture_probe import (
    CaptureDeviceProbe,
    LinuxCaptureDeviceProbe,
)
from aurora_core.hardware.models import (
    CaptureDeviceProbeResult,
    CaptureDeviceValidationReport,
)
from aurora_core.runtime.models import ComponentHealthState, ComponentId

_MESSAGES = {
    "capture_device_disabled": "Capture-device validation is disabled.",
    "unsupported_platform": "Capture-device validation is supported on Linux only.",
    "validated": (
        "Capture-device node presence and V4L2 registration metadata were validated."
    ),
}


def validate_capture_device(
    settings: AuroraSettings,
    probe: CaptureDeviceProbe | None = None,
    *,
    platform: str | None = None,
) -> CaptureDeviceValidationReport:
    if not settings.capture_device.enabled:
        return _report(
            CaptureDeviceProbeResult("capture_device_disabled"),
            ComponentHealthState.DISABLED,
        )
    if (sys.platform if platform is None else platform) != "linux":
        return _report(
            CaptureDeviceProbeResult("unsupported_platform"),
            ComponentHealthState.UNHEALTHY,
        )
    try:
        result = (LinuxCaptureDeviceProbe() if probe is None else probe).probe(
            identifier=settings.capture_device.identifier or ""
        )
    except Exception:
        result = CaptureDeviceProbeResult("probe_failed")
    return _report(
        result,
        ComponentHealthState.HEALTHY
        if result.reason_code == "validated"
        else ComponentHealthState.UNHEALTHY,
    )


def _report(
    result: CaptureDeviceProbeResult, state: ComponentHealthState
) -> CaptureDeviceValidationReport:
    return CaptureDeviceValidationReport(
        ComponentId.CAPTURE_DEVICE,
        state,
        result.reason_code,
        _MESSAGES.get(
            result.reason_code, "Capture-device presence validation was unsuccessful."
        ),
        result.device_node_present,
        result.character_device,
        result.v4l2_registered,
        result.process_read_access,
        result.device_name,
    )
