"""Synthetic tests for bounded V4L2 MMAP frame acquisition."""

from __future__ import annotations

import errno
import mmap
import os
import select
import stat
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest

from aurora_core.config.models import (
    AuroraSettings,
    CaptureDeviceSettings,
)
from aurora_core.hardware import v4l2_uapi as u
from aurora_core.hardware.capture_frame import validate_capture_frame
from aurora_core.hardware.capture_frame_streaming_probe import (
    LinuxV4L2AdaptiveCaptureFrameProbe,
    LinuxV4L2StreamingCaptureFrameProbe,
    _dequeued_frame_valid,
    _mapped_buffer_valid,
    _streaming_capabilities_valid,
    wipe_mapped_frame,
)
from aurora_core.hardware.models import CaptureFrameProbeResult
from aurora_core.runtime.models import ComponentHealthState

_FD = 52
_WIDTH = 1280
_HEIGHT = 720
_SIZEIMAGE = 8192
_MAPPED_LENGTH = 8192
_MAPPED_OFFSET = 4096
_FRAME_BYTES = 4096

_SETTINGS = AuroraSettings(
    capture_device=CaptureDeviceSettings(
        enabled=True,
        identifier="/dev/video0",
    )
)


class _Poll:
    def __init__(
        self,
        events: list[tuple[int, int]] | None = None,
    ) -> None:
        self.events = events if events is not None else [(_FD, select.POLLIN)]
        self.registrations: list[tuple[int, int]] = []
        self.timeouts: list[int] = []

    def register(self, fd: int, mask: int) -> None:
        self.registrations.append((fd, mask))

    def poll(self, timeout: int) -> list[tuple[int, int]]:
        self.timeouts.append(timeout)
        return self.events


class _MappedBuffer(bytearray):
    def __init__(self, length: int) -> None:
        super().__init__(b"x" * length)
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _FakeProbe:
    def __init__(self, result: CaptureFrameProbeResult) -> None:
        self.result = result
        self.calls = 0

    def probe(self, *, identifier: str) -> CaptureFrameProbeResult:
        assert identifier == "/dev/video0"
        self.calls += 1
        return self.result


def _good_ioctl_factory(
    requests: list[int],
    *,
    returned_buffer_count: int = 1,
    frame_bytes: int = _FRAME_BYTES,
):
    def ioctl(
        fd: int,
        request: int,
        buffer: bytearray,
        mutate: bool,
    ) -> int:
        assert fd == _FD
        assert mutate
        requests.append(request)

        if request == u.VIDIOC_QUERYCAP:
            buffer[84:88] = (u.CAP_VIDEO_CAPTURE | u.CAP_STREAMING).to_bytes(
                4, "little"
            )

        elif request == u.VIDIOC_G_FMT:
            buffer[8:12] = _WIDTH.to_bytes(4, "little")
            buffer[12:16] = _HEIGHT.to_bytes(4, "little")
            buffer[28:32] = _SIZEIMAGE.to_bytes(4, "little")

        elif request == u.VIDIOC_REQBUFS:
            requested_count = int.from_bytes(
                buffer[0:4],
                "little",
            )
            if requested_count == 1:
                buffer[0:4] = returned_buffer_count.to_bytes(
                    4,
                    "little",
                )

        elif request == u.VIDIOC_QUERYBUF:
            buffer[u.BUFFER_LENGTH_OFFSET : u.BUFFER_LENGTH_OFFSET + 4] = (
                _MAPPED_LENGTH.to_bytes(4, "little")
            )
            buffer[u.BUFFER_OFFSET_OFFSET : u.BUFFER_OFFSET_OFFSET + 4] = (
                _MAPPED_OFFSET.to_bytes(4, "little")
            )

        elif request == u.VIDIOC_DQBUF:
            buffer[u.BUFFER_BYTESUSED_OFFSET : u.BUFFER_BYTESUSED_OFFSET + 4] = (
                frame_bytes.to_bytes(4, "little")
            )

        elif request in {
            u.VIDIOC_QBUF,
            u.VIDIOC_STREAMON,
            u.VIDIOC_STREAMOFF,
        }:
            pass

        else:
            raise AssertionError(f"unexpected ioctl: {request:#x}")

        return 0

    return ioctl


def _make_probe(**overrides: Any) -> LinuxV4L2StreamingCaptureFrameProbe:
    requests: list[int] = []
    mapped = _MappedBuffer(_MAPPED_LENGTH)

    defaults: dict[str, Any] = {
        "abi_supported": lambda: True,
        "target_validator": lambda _identifier: (
            "synthetic",
            None,
        ),
        "opener": lambda _target, _flags: _FD,
        "fstat": lambda _fd: SimpleNamespace(st_mode=stat.S_IFCHR),
        "ioctl": _good_ioctl_factory(requests),
        "poll_factory": _Poll,
        "mapper": lambda *_args: mapped,
        "mapped_wiper": wipe_mapped_frame,
        "mapped_closer": lambda value: value.close(),
        "closer": lambda _fd: None,
        "monotonic": lambda: 0.0,
    }
    defaults.update(overrides)

    probe = LinuxV4L2StreamingCaptureFrameProbe(**defaults)
    probe.synthetic_requests = requests  # type: ignore[attr-defined]
    probe.synthetic_mapping = mapped  # type: ignore[attr-defined]
    return probe


def test_success_uses_one_mapped_buffer_and_complete_cleanup() -> None:
    probe = _make_probe()
    result = probe.probe(identifier="/dev/video0")

    assert result.reason_code == "validated"
    assert result.acquisition_method == "mmap"
    assert result.streaming_io_was_used
    assert result.frame_received
    assert result.frame_byte_count == _FRAME_BYTES
    assert result.current_width == _WIDTH
    assert result.current_height == _HEIGHT
    assert result.current_sizeimage == _SIZEIMAGE

    assert result.buffer_negotiation_succeeded
    assert result.buffer_was_mapped
    assert result.buffer_was_queued
    assert result.stream_was_started
    assert result.poll_was_attempted
    assert result.frame_dequeue_was_attempted
    assert result.stream_was_stopped
    assert result.frame_buffer_wipe_completed
    assert result.buffer_was_unmapped
    assert result.buffers_were_released
    assert result.descriptor_was_closed
    assert result.cleanup_completed

    assert probe.synthetic_requests == [  # type: ignore[attr-defined]
        u.VIDIOC_QUERYCAP,
        u.VIDIOC_G_FMT,
        u.VIDIOC_REQBUFS,
        u.VIDIOC_QUERYBUF,
        u.VIDIOC_QBUF,
        u.VIDIOC_STREAMON,
        u.VIDIOC_DQBUF,
        u.VIDIOC_STREAMOFF,
        u.VIDIOC_REQBUFS,
    ]

    mapping = probe.synthetic_mapping  # type: ignore[attr-defined]
    assert mapping == bytearray(_MAPPED_LENGTH)
    assert mapping.closed


def test_successful_streaming_result_is_healthy_through_service() -> None:
    report = validate_capture_frame(
        _SETTINGS,
        _make_probe(),
        platform="linux",
    )

    assert report.state is ComponentHealthState.HEALTHY
    assert report.reason_code == "validated"
    assert report.acquisition_method == "mmap"
    assert report.streaming_io_was_used


def test_adaptive_probe_falls_back_only_for_missing_readwrite() -> None:
    readwrite = _FakeProbe(
        CaptureFrameProbeResult(
            "readwrite_io_not_supported",
            device_was_opened=True,
            descriptor_was_closed=True,
            capability_query_succeeded=True,
            cleanup_completed=True,
        )
    )
    streaming_result = CaptureFrameProbeResult("streaming-result")
    streaming = _FakeProbe(streaming_result)

    result = LinuxV4L2AdaptiveCaptureFrameProbe(
        readwrite_probe=readwrite,
        streaming_probe=streaming,
    ).probe(identifier="/dev/video0")

    assert result is streaming_result
    assert readwrite.calls == 1
    assert streaming.calls == 1


def test_adaptive_probe_preserves_nonfallback_failure() -> None:
    original = CaptureFrameProbeResult("device_busy")
    readwrite = _FakeProbe(original)
    streaming = _FakeProbe(CaptureFrameProbeResult("must-not-run"))

    result = LinuxV4L2AdaptiveCaptureFrameProbe(
        readwrite_probe=readwrite,
        streaming_probe=streaming,
    ).probe(identifier="/dev/video0")

    assert result is original
    assert readwrite.calls == 1
    assert streaming.calls == 0


def test_open_uses_nonblocking_readwrite_flags() -> None:
    seen: list[int] = []

    def opener(_target: str, flags: int) -> int:
        seen.append(flags)
        return _FD

    result = _make_probe(opener=opener).probe(identifier="/dev/video0")

    assert result.reason_code == "validated"
    assert seen[0] & os.O_RDWR
    assert seen[0] & os.O_NONBLOCK
    assert not seen[0] & getattr(os, "O_CREAT", 0)


def test_missing_streaming_capability_stops_after_querycap() -> None:
    requests: list[int] = []

    def ioctl(
        _fd: int,
        request: int,
        buffer: bytearray,
        _mutate: bool,
    ) -> int:
        requests.append(request)
        buffer[84:88] = u.CAP_VIDEO_CAPTURE.to_bytes(
            4,
            "little",
        )
        return 0

    result = _make_probe(ioctl=ioctl).probe(identifier="/dev/video0")

    assert result.reason_code == "streaming_io_not_supported"
    assert requests == [u.VIDIOC_QUERYCAP]
    assert result.descriptor_was_closed
    assert result.cleanup_completed


def test_poll_timeout_stops_wipes_unmaps_releases_and_closes() -> None:
    result = _make_probe(
        poll_factory=lambda: _Poll(events=[]),
    ).probe(identifier="/dev/video0")

    assert result.reason_code == "poll_timeout"
    assert not result.frame_received
    assert result.stream_was_started
    assert result.stream_was_stopped
    assert result.frame_buffer_wipe_completed
    assert result.buffer_was_unmapped
    assert result.buffers_were_released
    assert result.descriptor_was_closed
    assert result.cleanup_completed


def test_map_failure_releases_negotiated_buffers() -> None:
    result = _make_probe(
        mapper=lambda *_args: (_ for _ in ()).throw(OSError(errno.ENOMEM, "private"))
    ).probe(identifier="/dev/video0")

    assert result.reason_code == "buffer_map_failed"
    assert result.buffer_negotiation_succeeded
    assert not result.buffer_was_mapped
    assert result.buffers_were_released
    assert result.descriptor_was_closed
    assert result.cleanup_completed
    assert "private" not in repr(result)


def test_driver_buffer_count_is_bounded_and_released() -> None:
    requests: list[int] = []
    result = _make_probe(
        ioctl=_good_ioctl_factory(
            requests,
            returned_buffer_count=9,
        )
    ).probe(identifier="/dev/video0")

    assert result.reason_code == "buffer_count_exceeds_limit"
    assert result.buffer_negotiation_succeeded
    assert result.buffers_were_released
    assert result.descriptor_was_closed
    assert result.cleanup_completed


@pytest.mark.parametrize(
    "frame_bytes, expected",
    [
        (0, "frame_empty"),
        (_SIZEIMAGE + 1, "frame_byte_count_invalid"),
    ],
)
def test_invalid_dequeued_byte_count_still_cleans_up(
    frame_bytes: int,
    expected: str,
) -> None:
    requests: list[int] = []
    result = _make_probe(
        ioctl=_good_ioctl_factory(
            requests,
            frame_bytes=frame_bytes,
        )
    ).probe(identifier="/dev/video0")

    assert result.reason_code == expected
    assert not result.frame_received
    assert result.frame_byte_count == frame_bytes
    assert result.frame_buffer_wipe_completed
    assert result.stream_was_stopped
    assert result.buffer_was_unmapped
    assert result.buffers_were_released
    assert result.cleanup_completed


def test_mapped_frame_wiper_overwrites_entire_buffer() -> None:
    buffer = mmap.mmap(-1, 131_073)
    try:
        buffer[:] = b"s" * len(buffer)
        assert wipe_mapped_frame(buffer)
        assert buffer[:] == b"\0" * len(buffer)
    finally:
        buffer.close()


def test_streaming_capability_parser_uses_device_caps() -> None:
    response = bytearray(104)
    response[84:88] = (u.CAP_DEVICE_CAPS | u.CAP_VIDEO_CAPTURE).to_bytes(4, "little")
    response[88:92] = (u.CAP_VIDEO_CAPTURE | u.CAP_STREAMING).to_bytes(4, "little")

    assert _streaming_capabilities_valid(response) == (
        True,
        True,
    )


def test_mapped_buffer_parser_rejects_excessive_length() -> None:
    response = u.capture_buffer_request()
    response[u.BUFFER_LENGTH_OFFSET : u.BUFFER_LENGTH_OFFSET + 4] = (
        16 * 1024 * 1024 + 1
    ).to_bytes(4, "little")

    assert _mapped_buffer_valid(response) == "mapped_buffer_length_exceeds_limit"


def test_dequeued_frame_parser_requires_matching_buffer() -> None:
    response = u.capture_buffer_request(index=1)

    assert _dequeued_frame_valid(
        response,
        current_sizeimage=_SIZEIMAGE,
        mapped_length=_MAPPED_LENGTH,
    ) == (None, "invalid_frame_dequeue_response")


def test_stream_cleanup_failure_is_consistent_unhealthy() -> None:
    valid = CaptureFrameProbeResult(
        reason_code="validated",
        device_was_opened=True,
        descriptor_was_closed=True,
        capability_query_succeeded=True,
        current_format_query_succeeded=True,
        acquisition_method="mmap",
        poll_was_attempted=True,
        frame_received=True,
        frame_byte_count=_FRAME_BYTES,
        current_width=_WIDTH,
        current_height=_HEIGHT,
        current_sizeimage=_SIZEIMAGE,
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

    failed = replace(
        valid,
        reason_code="buffer_release_failed",
        cleanup_completed=False,
        buffers_were_released=False,
    )

    report = validate_capture_frame(
        _SETTINGS,
        _FakeProbe(failed),
        platform="linux",
    )

    assert report.state is ComponentHealthState.UNHEALTHY
    assert report.reason_code == "buffer_release_failed"
