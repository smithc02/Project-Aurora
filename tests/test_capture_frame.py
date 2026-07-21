"""Synthetic safety checks for bounded capture-frame validation."""

from __future__ import annotations

import stat

from aurora_core.config.models import AuroraSettings, CaptureDeviceSettings
from aurora_core.hardware.capture_frame import validate_capture_frame
from aurora_core.hardware.capture_frame_probe import (
    LinuxV4L2CaptureFrameProbe,
    _capabilities_valid,
    _format_valid,
)
from aurora_core.hardware.models import CaptureFrameProbeResult
from aurora_core.hardware.v4l2_uapi import (
    CAP_READWRITE,
    CAP_VIDEO_CAPTURE,
    capture_format_request,
)
from aurora_core.runtime.models import ComponentHealthState


def test_disabled_performs_no_probe() -> None:
    class Forbidden:
        def probe(self, *, identifier: str) -> CaptureFrameProbeResult:
            raise AssertionError(identifier)

    report = validate_capture_frame(
        AuroraSettings(capture_device=CaptureDeviceSettings(enabled=False)), Forbidden()
    )
    assert report.state is ComponentHealthState.DISABLED
    assert report.reason_code == "capture_device_disabled"


def test_unsupported_platform_performs_no_probe() -> None:
    report = validate_capture_frame(
        AuroraSettings(
            capture_device=CaptureDeviceSettings(enabled=True, identifier="/dev/video0")
        ),
        platform="darwin",
    )
    assert report.reason_code == "unsupported_platform"


def test_uapi_request_is_complete_and_zeroed() -> None:
    request = capture_format_request()
    assert len(request) == 208
    assert request[:4] == b"\x01\0\0\0"
    assert request[4:] == b"\0" * 204


def test_parsers_ignore_capability_strings_and_validate_format() -> None:
    cap = bytearray(104)
    cap[:80] = (b"secret-canary" * 7)[:80]
    cap[84:88] = (CAP_VIDEO_CAPTURE | CAP_READWRITE).to_bytes(4, "little")
    assert _capabilities_valid(cap) == (True, True)
    fmt = capture_format_request()
    fmt[8:12] = (640).to_bytes(4, "little")
    fmt[12:16] = (480).to_bytes(4, "little")
    fmt[28:32] = (1024).to_bytes(4, "little")
    assert _format_valid(fmt) == (640, 480, 1024)


def test_synthetic_success_wipes_and_closes() -> None:
    calls: list[str] = []

    class Poll:
        def register(self, fd: int, mask: int) -> None:
            calls.append("register")

        def poll(self, timeout: int) -> list[tuple[int, int]]:
            return [(7, 1)]

    def ioctl(fd: int, request: int, buffer: bytearray, mutate: bool) -> int:
        calls.append("ioctl")
        if len(buffer) == 104:
            buffer[84:88] = (CAP_VIDEO_CAPTURE | CAP_READWRITE).to_bytes(4, "little")
        else:
            buffer[8:12] = (1).to_bytes(4, "little")
            buffer[12:16] = (1).to_bytes(4, "little")
            buffer[28:32] = (4).to_bytes(4, "little")
        return 0

    probe = LinuxV4L2CaptureFrameProbe(
        abi_supported=lambda: True,
        target_validator=lambda _: ("synthetic", None),
        opener=lambda *_: 7,
        fstat=lambda _: type("S", (), {"st_mode": stat.S_IFCHR})(),
        ioctl=ioctl,
        poll_factory=Poll,
        reader=lambda _, buffer: buffer.__setitem__(slice(None), b"test") or 4,
        closer=lambda _: calls.append("close"),
        monotonic=lambda: 0,
    )
    result = probe.probe(identifier="synthetic")
    assert result.reason_code == "validated"
    assert (
        result.frame_received
        and result.frame_buffer_wipe_completed
        and result.cleanup_completed
    )
    assert (
        result.descriptor_was_closed
        and calls.count("ioctl") == 2
        and calls[-1] == "close"
    )
