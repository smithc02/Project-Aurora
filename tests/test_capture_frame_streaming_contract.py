"""Synthetic service-contract tests for bounded MMAP frame acquisition."""

from __future__ import annotations

from dataclasses import replace

import pytest

from aurora_core.__main__ import _print_capture_frame_report
from aurora_core.config.models import AuroraSettings, CaptureDeviceSettings
from aurora_core.hardware.capture_frame import validate_capture_frame
from aurora_core.hardware.models import CaptureFrameProbeResult
from aurora_core.runtime.models import ComponentHealthState

_SETTINGS = AuroraSettings(
    capture_device=CaptureDeviceSettings(enabled=True, identifier="/dev/video0")
)


class _Probe:
    def __init__(self, result: CaptureFrameProbeResult) -> None:
        self.result = result

    def probe(self, *, identifier: str) -> CaptureFrameProbeResult:
        assert identifier == "/dev/video0"
        return self.result


def _valid_streaming_result() -> CaptureFrameProbeResult:
    return CaptureFrameProbeResult(
        reason_code="validated",
        device_was_opened=True,
        descriptor_was_closed=True,
        capability_query_succeeded=True,
        current_format_query_succeeded=True,
        acquisition_method="mmap",
        poll_was_attempted=True,
        frame_read_was_attempted=False,
        frame_received=True,
        frame_byte_count=4096,
        current_width=1280,
        current_height=720,
        current_sizeimage=1_048_576,
        frame_buffer_wipe_completed=True,
        cleanup_completed=True,
        streaming_io_was_used=True,
        buffer_negotiation_succeeded=True,
        buffer_was_mapped=True,
        buffer_was_queued=True,
        stream_was_started=True,
        frame_dequeue_was_attempted=True,
        stream_was_stopped=True,
        buffer_was_unmapped=True,
        buffers_were_released=True,
    )


def test_complete_streaming_result_is_healthy() -> None:
    report = validate_capture_frame(
        _SETTINGS,
        _Probe(_valid_streaming_result()),
        platform="linux",
    )

    assert report.state is ComponentHealthState.HEALTHY
    assert report.reason_code == "validated"
    assert report.acquisition_method == "mmap"
    assert report.streaming_io_was_used
    assert report.frame_received
    assert report.cleanup_completed


@pytest.mark.parametrize(
    "field",
    [
        "buffer_negotiation_succeeded",
        "buffer_was_mapped",
        "buffer_was_queued",
        "stream_was_started",
        "frame_dequeue_was_attempted",
        "stream_was_stopped",
        "buffer_was_unmapped",
        "buffers_were_released",
    ],
)
def test_missing_streaming_success_event_is_rejected(field: str) -> None:
    result = replace(_valid_streaming_result(), **{field: False})

    report = validate_capture_frame(_SETTINGS, _Probe(result), platform="linux")

    assert report.state is ComponentHealthState.UNHEALTHY
    assert report.reason_code == "unexpected_probe_failure"


@pytest.mark.parametrize(
    "changes",
    [
        {"streaming_io_was_used": False},
        {"frame_read_was_attempted": True},
        {"poll_was_attempted": False},
        {"frame_byte_count": None},
        {"frame_byte_count": 1_048_577},
        {"frame_buffer_wipe_completed": False},
        {"cleanup_completed": False},
    ],
)
def test_inconsistent_streaming_result_is_rejected(
    changes: dict[str, object],
) -> None:
    result = replace(_valid_streaming_result(), **changes)

    report = validate_capture_frame(_SETTINGS, _Probe(result), platform="linux")

    assert report.state is ComponentHealthState.UNHEALTHY
    assert report.reason_code == "unexpected_probe_failure"


def test_streaming_contract_is_visible_in_cli(
    capsys: pytest.CaptureFixture[str],
) -> None:
    report = validate_capture_frame(
        _SETTINGS,
        _Probe(_valid_streaming_result()),
        platform="linux",
    )

    _print_capture_frame_report(report)
    output = capsys.readouterr().out

    for expected in (
        "acquisition_method: mmap",
        "streaming_io_was_used: yes",
        "buffer_negotiation_succeeded: yes",
        "buffer_was_mapped: yes",
        "buffer_was_queued: yes",
        "stream_was_started: yes",
        "frame_dequeue_attempted: yes",
        "stream_was_stopped: yes",
        "buffer_was_unmapped: yes",
        "buffers_were_released: yes",
    ):
        assert expected in output

    assert "No frame content was retained, printed, or transmitted." in output
    assert (
        "One bounded V4L2 MMAP acquisition may have requested driver buffers" in output
    )
