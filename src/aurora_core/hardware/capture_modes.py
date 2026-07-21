"""Service for explicit bounded query-only capture mode enumeration."""

from __future__ import annotations

import sys

from aurora_core.config.models import AuroraSettings
from aurora_core.hardware.capture_modes_probe import (
    CaptureModeProbe,
    LinuxV4L2ModeProbe,
)
from aurora_core.hardware.models import (
    CaptureModeProbeResult,
    CaptureModeValidationReport,
)
from aurora_core.runtime.models import ComponentHealthState, ComponentId


def validate_capture_modes(
    settings: AuroraSettings,
    probe: CaptureModeProbe | None = None,
    *,
    platform: str | None = None,
) -> CaptureModeValidationReport:
    if not settings.capture_device.enabled:
        return _report(
            CaptureModeProbeResult("capture_device_disabled"),
            ComponentHealthState.DISABLED,
        )
    if (sys.platform if platform is None else platform) != "linux":
        return _report(
            CaptureModeProbeResult("unsupported_platform"),
            ComponentHealthState.UNHEALTHY,
        )
    try:
        result = (LinuxV4L2ModeProbe() if probe is None else probe).enumerate_modes(
            identifier=settings.capture_device.identifier or ""
        )
    except Exception:
        result = CaptureModeProbeResult("format_enumeration_failed")
    state = (
        ComponentHealthState.HEALTHY
        if result.reason_code == "validated" and result.formats
        else (
            ComponentHealthState.DEGRADED
            if result.formats
            else ComponentHealthState.UNHEALTHY
        )
    )
    return _report(result, state)


def _report(
    r: CaptureModeProbeResult, s: ComponentHealthState
) -> CaptureModeValidationReport:
    sizes = tuple(x for f in r.formats for x in f.frame_sizes)
    intervals = sum((len(x.intervals) for x in sizes), 0)
    return CaptureModeValidationReport(
        ComponentId.CAPTURE_DEVICE,
        s,
        r.reason_code,
        "Capture mode enumeration completed."
        if s is ComponentHealthState.HEALTHY
        else "Capture mode enumeration was incomplete or unsuccessful.",
        r.formats,
        len(r.formats),
        len(sizes),
        intervals,
        r.enumeration_complete,
        r.partial_reason_codes,
        r.device_was_opened,
        r.querycap_was_issued,
        r.enumeration_ioctl_was_issued,
        r.descriptor_was_closed,
    )
