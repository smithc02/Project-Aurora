"""Service boundary for the explicit bounded capture-frame command."""

from __future__ import annotations

import sys

from aurora_core.config.models import AuroraSettings
from aurora_core.hardware.capture_frame_probe import (
    CaptureFrameProbe,
    LinuxV4L2CaptureFrameProbe,
)
from aurora_core.hardware.models import (
    CaptureFrameProbeResult,
    CaptureFrameValidationReport,
)
from aurora_core.runtime.models import ComponentHealthState, ComponentId


def validate_capture_frame(
    settings: AuroraSettings,
    probe: CaptureFrameProbe | None = None,
    *,
    platform: str | None = None,
) -> CaptureFrameValidationReport:
    if not settings.capture_device.enabled:
        result = CaptureFrameProbeResult("capture_device_disabled")
        state = ComponentHealthState.DISABLED
    elif (sys.platform if platform is None else platform) != "linux":
        result = CaptureFrameProbeResult("unsupported_platform")
        state = ComponentHealthState.UNHEALTHY
    else:
        try:
            result = (probe or LinuxV4L2CaptureFrameProbe()).probe(
                identifier=settings.capture_device.identifier or ""
            )
        except Exception:
            result = CaptureFrameProbeResult("unexpected_probe_failure")
        state = (
            ComponentHealthState.HEALTHY
            if result.reason_code == "validated"
            and result.frame_received
            and result.cleanup_completed
            else (
                ComponentHealthState.DEGRADED
                if result.reason_code == "frame_received_cleanup_unconfirmed"
                else ComponentHealthState.UNHEALTHY
            )
        )
    return CaptureFrameValidationReport(
        ComponentId.CAPTURE_DEVICE,
        state,
        result.reason_code,
        "Bounded single-frame validation completed."
        if state is ComponentHealthState.HEALTHY
        else "Bounded single-frame validation was unsuccessful.",
        result.device_was_opened,
        result.descriptor_was_closed,
        result.capability_query_succeeded,
        result.current_format_query_succeeded,
        result.acquisition_method,
        result.poll_was_attempted,
        result.frame_read_was_attempted,
        result.frame_received,
        result.frame_byte_count,
        result.current_width,
        result.current_height,
        result.current_sizeimage,
        result.frame_buffer_wipe_completed,
        result.cleanup_completed,
        False,
    )
