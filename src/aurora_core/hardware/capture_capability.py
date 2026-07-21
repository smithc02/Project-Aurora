"""One-shot query-only V4L2 capability validation service."""

from __future__ import annotations

import sys
from dataclasses import replace

from aurora_core.config.models import AuroraSettings
from aurora_core.hardware.capture_capability_probe import (
    CaptureCapabilityProbe,
    LinuxV4L2CapabilityProbe,
)
from aurora_core.hardware.models import (
    CaptureCapabilityProbeResult,
    CaptureCapabilityValidationReport,
)
from aurora_core.runtime.models import ComponentHealthState, ComponentId

_MESSAGES = {
    "capture_device_disabled": "Capture capability validation is disabled.",
    "unsupported_platform": "Capture capability validation is supported on Linux only.",
    "validated": "The V4L2 capability response was valid.",
}


def validate_capture_capability(
    settings: AuroraSettings,
    probe: CaptureCapabilityProbe | None = None,
    *,
    platform: str | None = None,
) -> CaptureCapabilityValidationReport:
    if not settings.capture_device.enabled:
        return _report(
            CaptureCapabilityProbeResult("capture_device_disabled"),
            ComponentHealthState.DISABLED,
        )
    if (sys.platform if platform is None else platform) != "linux":
        return _report(
            CaptureCapabilityProbeResult("unsupported_platform"),
            ComponentHealthState.UNHEALTHY,
        )
    try:
        result = (LinuxV4L2CapabilityProbe() if probe is None else probe).query(
            identifier=settings.capture_device.identifier or ""
        )
    except Exception:
        result = CaptureCapabilityProbeResult("capability_query_failed")
    if result.reason_code == "validated":
        if not (result.single_planar_capture or result.multi_planar_capture):
            result = replace(result, reason_code="video_capture_not_supported")
        elif not (result.streaming_io or result.readwrite_io):
            result = replace(result, reason_code="capture_io_method_missing")
    return _report(
        result,
        ComponentHealthState.HEALTHY
        if result.reason_code == "validated"
        else ComponentHealthState.UNHEALTHY,
    )


def _report(
    result: CaptureCapabilityProbeResult, state: ComponentHealthState
) -> CaptureCapabilityValidationReport:
    return CaptureCapabilityValidationReport(
        ComponentId.CAPTURE_DEVICE,
        state,
        result.reason_code,
        _MESSAGES.get(
            result.reason_code, "Capture capability validation was unsuccessful."
        ),
        result.query_succeeded,
        result.driver_name,
        result.card_name,
        result.v4l2_api_version,
        result.single_planar_capture,
        result.multi_planar_capture,
        result.streaming_io,
        result.readwrite_io,
        result.device_was_opened,
        result.ioctl_was_issued,
        result.descriptor_was_closed,
    )
