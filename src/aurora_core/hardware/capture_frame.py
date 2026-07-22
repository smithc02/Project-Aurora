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

_MAX_WIDTH = 8192
_MAX_HEIGHT = 8192
_MAX_SIZEIMAGE = 8 * 1024 * 1024


def _bounded_frame_metadata(result: CaptureFrameProbeResult) -> bool:
    return (
        result.frame_byte_count is not None
        and result.current_width is not None
        and result.current_height is not None
        and result.current_sizeimage is not None
        and 1 <= result.current_width <= _MAX_WIDTH
        and 1 <= result.current_height <= _MAX_HEIGHT
        and 1 <= result.current_sizeimage <= _MAX_SIZEIMAGE
        and 1 <= result.frame_byte_count <= result.current_sizeimage
    )


def _healthy_result(result: CaptureFrameProbeResult) -> bool:
    return (
        result.reason_code == "validated"
        and result.acquisition_method == "readwrite"
        and result.frame_received
        and _bounded_frame_metadata(result)
        and result.frame_buffer_wipe_completed
        and result.descriptor_was_closed
        and result.cleanup_completed
        and not result.streaming_io_was_used
    )


def _degraded_result(result: CaptureFrameProbeResult) -> bool:
    return (
        result.reason_code == "frame_received_cleanup_unconfirmed"
        and result.acquisition_method == "readwrite"
        and result.frame_received
        and _bounded_frame_metadata(result)
        and result.frame_buffer_wipe_completed
        and not result.descriptor_was_closed
        and not result.cleanup_completed
        and not result.streaming_io_was_used
    )


def _consistent_result(result: CaptureFrameProbeResult) -> bool:
    dimensions = (
        result.current_width,
        result.current_height,
        result.current_sizeimage,
    )
    dimensions_are_complete = all(value is not None for value in dimensions)
    dimensions_are_absent = all(value is None for value in dimensions)
    acquisition_is_readwrite = result.acquisition_method == "readwrite"
    if result.streaming_io_was_used:
        return False
    if result.acquisition_method not in {None, "readwrite"}:
        return False
    if not result.device_was_opened:
        return not any(
            (
                result.descriptor_was_closed,
                result.capability_query_succeeded,
                result.current_format_query_succeeded,
                acquisition_is_readwrite,
                result.poll_was_attempted,
                result.frame_read_was_attempted,
                result.frame_received,
                result.frame_byte_count is not None,
                not dimensions_are_absent,
                result.frame_buffer_wipe_completed,
                result.cleanup_completed,
            )
        )
    if result.current_format_query_succeeded and not result.capability_query_succeeded:
        return False
    if not (dimensions_are_complete or dimensions_are_absent):
        return False
    if dimensions_are_complete:
        assert result.current_width is not None
        assert result.current_height is not None
        assert result.current_sizeimage is not None
        if not result.current_format_query_succeeded or not (
            1 <= result.current_width <= _MAX_WIDTH
            and 1 <= result.current_height <= _MAX_HEIGHT
            and 1 <= result.current_sizeimage <= _MAX_SIZEIMAGE
        ):
            return False
    if acquisition_is_readwrite:
        if not result.current_format_query_succeeded or not dimensions_are_complete:
            return False
    elif result.poll_was_attempted or result.frame_read_was_attempted:
        return False
    if result.frame_read_was_attempted and not result.poll_was_attempted:
        return False
    if result.poll_was_attempted and not acquisition_is_readwrite:
        return False
    if result.frame_byte_count is not None and not result.frame_read_was_attempted:
        return False
    if (
        not result.frame_received
        and result.frame_byte_count is not None
        and result.current_sizeimage is not None
        and 1 <= result.frame_byte_count <= result.current_sizeimage
    ):
        return False
    if result.frame_received:
        if not result.frame_read_was_attempted or not _bounded_frame_metadata(result):
            return False
    if result.frame_buffer_wipe_completed and not acquisition_is_readwrite:
        return False
    expected_cleanup = result.descriptor_was_closed and (
        not acquisition_is_readwrite or result.frame_buffer_wipe_completed
    )
    if result.cleanup_completed != expected_cleanup:
        return False
    if result.reason_code == "validated":
        return _healthy_result(result)
    if result.reason_code == "frame_received_cleanup_unconfirmed":
        return _degraded_result(result)
    if result.frame_received and result.frame_buffer_wipe_completed:
        return False
    if acquisition_is_readwrite and not result.frame_buffer_wipe_completed:
        return result.reason_code == "frame_buffer_wipe_failed"
    if result.reason_code == "frame_buffer_wipe_failed":
        return False
    if not result.descriptor_was_closed:
        return result.reason_code == "descriptor_close_failed"
    if result.reason_code == "descriptor_close_failed":
        return False
    return True


def validate_capture_frame(
    settings: AuroraSettings,
    probe: CaptureFrameProbe | None = None,
    *,
    platform: str | None = None,
) -> CaptureFrameValidationReport:
    selected_platform = sys.platform if platform is None else platform
    if not settings.capture_device.enabled:
        result = CaptureFrameProbeResult("capture_device_disabled")
        reason_code = result.reason_code
        state = ComponentHealthState.DISABLED
    elif selected_platform != "linux":
        result = CaptureFrameProbeResult("unsupported_platform")
        reason_code = result.reason_code
        state = ComponentHealthState.UNHEALTHY
    else:
        try:
            result = (probe or LinuxV4L2CaptureFrameProbe()).probe(
                identifier=settings.capture_device.identifier or ""
            )
        except Exception:
            result = CaptureFrameProbeResult("unexpected_probe_failure")
        if not _consistent_result(result):
            reason_code = "unexpected_probe_failure"
            state = ComponentHealthState.UNHEALTHY
        elif _healthy_result(result):
            reason_code = result.reason_code
            state = ComponentHealthState.HEALTHY
        elif _degraded_result(result):
            reason_code = result.reason_code
            state = ComponentHealthState.DEGRADED
        else:
            reason_code = result.reason_code
            state = ComponentHealthState.UNHEALTHY
    return CaptureFrameValidationReport(
        component_id=ComponentId.CAPTURE_DEVICE,
        state=state,
        reason_code=reason_code,
        message="Bounded single-frame validation completed."
        if state is ComponentHealthState.HEALTHY
        else "Bounded single-frame validation was unsuccessful.",
        device_was_opened=result.device_was_opened,
        descriptor_was_closed=result.descriptor_was_closed,
        capability_query_succeeded=result.capability_query_succeeded,
        current_format_query_succeeded=result.current_format_query_succeeded,
        acquisition_method=result.acquisition_method,
        poll_was_attempted=result.poll_was_attempted,
        frame_read_was_attempted=result.frame_read_was_attempted,
        frame_received=result.frame_received,
        frame_byte_count=result.frame_byte_count,
        current_width=result.current_width,
        current_height=result.current_height,
        current_sizeimage=result.current_sizeimage,
        frame_buffer_wipe_completed=result.frame_buffer_wipe_completed,
        cleanup_completed=result.cleanup_completed,
        streaming_io_was_used=result.streaming_io_was_used,
    )
