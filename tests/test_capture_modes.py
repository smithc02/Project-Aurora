"""Synthetic tests for bounded query-only V4L2 mode enumeration."""

from __future__ import annotations

import errno
import stat
from types import SimpleNamespace

import pytest

from aurora_core.config import load_settings
from aurora_core.hardware import v4l2_uapi as u
from aurora_core.hardware.capture_modes import validate_capture_modes
from aurora_core.hardware.capture_modes_probe import LinuxV4L2ModeProbe
from aurora_core.hardware.models import (
    CaptureModeProbeResult,
    CaptureModeValidationReport,
    CapturePixelFormat,
)
from aurora_core.runtime.models import ComponentHealthState, ComponentId


def test_uapi_layouts_and_requests() -> None:
    assert u._FM.size == 64 and u._FS.size == 44 and u._FI.size == 52
    assert u.VIDIOC_ENUM_FMT == 0xC0405602
    assert u.VIDIOC_ENUM_FRAMESIZES == 0xC02C564A
    assert u.VIDIOC_ENUM_FRAMEINTERVALS == 0xC034564B
    assert u.VIDIOC_QUERYCAP == 0x80685600


def test_probe_enumerates_synthetic_discrete_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aurora_core.hardware import capture_modes_probe as m

    monkeypatch.setattr(m, "_validated_video_target", lambda _: ("/dev/video0", None))
    monkeypatch.setattr(m.os, "open", lambda path, flags: 9)
    monkeypatch.setattr(m.os, "fstat", lambda fd: SimpleNamespace(st_mode=stat.S_IFCHR))
    closed: list[int] = []
    monkeypatch.setattr(m.os, "close", lambda fd: closed.append(fd))
    calls: list[int] = []

    def ioctl(fd: int, request: int, b: bytearray, mutate: bool) -> int:
        calls.append(request)
        if request == u.VIDIOC_QUERYCAP:
            b[84:88] = (u.CAP_VIDEO_CAPTURE).to_bytes(4, "little")
            return 0
        idx = int.from_bytes(b[:4], "little")
        if request == u.VIDIOC_ENUM_FMT:
            if idx:
                raise OSError(errno.EINVAL, "end")
            b[:] = u._FM.pack(
                0,
                u.VIDEO_CAPTURE,
                3,
                b"Motion-JPEG\0" + b"\0" * 20,
                int.from_bytes(b"MJPG", "little"),
                0,
                0,
                0,
                0,
            )
            return 0
        if request == u.VIDIOC_ENUM_FRAMESIZES:
            if idx:
                raise OSError(errno.EINVAL, "end")
            b[:] = u._FS.pack(
                0, int.from_bytes(b"MJPG", "little"), 1, 1920, 1080, 0, 0, 0, 0, 0, 0
            )
            return 0
        if request == u.VIDIOC_ENUM_FRAMEINTERVALS:
            if idx:
                raise OSError(errno.EINVAL, "end")
            b[:] = u._FI.pack(
                0,
                int.from_bytes(b"MJPG", "little"),
                1920,
                1080,
                1,
                1,
                60,
                0,
                0,
                0,
                0,
                0,
                0,
            )
            return 0
        raise AssertionError

    monkeypatch.setattr(m.fcntl, "ioctl", ioctl)
    r = LinuxV4L2ModeProbe().enumerate_modes(identifier="/dev/video0")
    assert (
        r.reason_code == "validated"
        and r.enumeration_complete
        and r.descriptor_was_closed
    )
    assert (
        r.formats[0].fourcc == "MJPG"
        and r.formats[0].frame_sizes[0].intervals[0].denominator == 60
    )
    assert closed == [9] and calls.count(u.VIDIOC_QUERYCAP) == 1


def test_probe_range_is_degraded_and_no_interval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aurora_core.hardware import capture_modes_probe as m

    monkeypatch.setattr(m, "_validated_video_target", lambda _: ("/dev/video0", None))
    monkeypatch.setattr(m.os, "open", lambda p, f: 3)
    monkeypatch.setattr(m.os, "fstat", lambda _: SimpleNamespace(st_mode=stat.S_IFCHR))
    monkeypatch.setattr(m.os, "close", lambda _: None)

    def ioctl(fd: int, request: int, b: bytearray, mutate: bool) -> int:
        i = int.from_bytes(b[:4], "little")
        if request == u.VIDIOC_QUERYCAP:
            b[84:88] = u.CAP_VIDEO_CAPTURE.to_bytes(4, "little")
        elif request == u.VIDIOC_ENUM_FMT:
            if i:
                raise OSError(errno.EINVAL, "x")
            b[:] = u._FM.pack(
                0,
                1,
                0,
                b"Raw\0" + b"\0" * 28,
                int.from_bytes(b"YUYV", "little"),
                0,
                0,
                0,
                0,
            )
        elif request == u.VIDIOC_ENUM_FRAMESIZES:
            b[:] = u._FS.pack(
                0, int.from_bytes(b"YUYV", "little"), 3, 1, 2, 1, 1, 2, 1, 0, 0
            )
        else:
            raise AssertionError("interval must not be called")
        return 0

    monkeypatch.setattr(m.fcntl, "ioctl", ioctl)
    r = LinuxV4L2ModeProbe().enumerate_modes(identifier="x")
    assert (
        r.reason_code == "validated_with_gaps"
        and r.formats[0].frame_sizes[0].kind == "stepwise"
    )


class Fake:
    def __init__(self, r: CaptureModeProbeResult):
        self.r = r
        self.calls = 0

    def enumerate_modes(self, *, identifier: str) -> CaptureModeProbeResult:
        self.calls += 1
        return self.r


def test_service_states() -> None:
    disabled = Fake(CaptureModeProbeResult("validated"))
    assert (
        validate_capture_modes(load_settings(environment={}), disabled).state
        is ComponentHealthState.DISABLED
        and disabled.calls == 0
    )
    settings = load_settings(
        environment={},
        cli_overrides={
            "capture_device": {"enabled": True, "identifier": "/dev/video0"}
        },
    )
    fmt = CapturePixelFormat("single_planar", "YUYV", False, None, False, False)
    assert (
        validate_capture_modes(
            settings,
            Fake(CaptureModeProbeResult("validated", (fmt,), True)),
            platform="linux",
        ).state
        is ComponentHealthState.UNHEALTHY
    )
    assert (
        validate_capture_modes(
            settings,
            Fake(
                CaptureModeProbeResult(
                    "validated_with_gaps",
                    (fmt,),
                    False,
                    ("gap",),
                    True,
                    True,
                    True,
                    True,
                )
            ),
            platform="linux",
        ).state
        is ComponentHealthState.DEGRADED
    )


def test_mode_report_printer_is_sanitized(capsys: pytest.CaptureFixture[str]) -> None:
    from aurora_core.__main__ import _print_capture_modes_report
    from aurora_core.hardware.models import (
        CaptureFrameInterval,
        CaptureFrameSize,
        CaptureModeValidationReport,
    )

    interval = CaptureFrameInterval("discrete", 1001, 60000)
    size = CaptureFrameSize(
        "discrete", 1920, 1080, intervals=(interval,), intervals_enumerated=True
    )
    fmt = CapturePixelFormat(
        "single_planar", "MJPG", False, "Motion-JPEG", True, False, (size,)
    )
    report = CaptureModeValidationReport(
        ComponentId.CAPTURE_DEVICE,
        ComponentHealthState.HEALTHY,
        "validated",
        "safe",
        (fmt,),
        1,
        1,
        1,
        True,
        (),
        True,
        True,
        True,
        True,
    )
    _print_capture_modes_report(report)
    output = capsys.readouterr().out
    assert "MJPG" in output and "1001/60000" in output and "/dev/video" not in output


def test_private_normalizers_and_bounds(monkeypatch: pytest.MonkeyPatch) -> None:
    from aurora_core.hardware import capture_modes_probe as m

    probe = LinuxV4L2ModeProbe()
    assert probe._fourcc(int.from_bytes(b"YUYV", "little")) == ("YUYV", False)
    assert probe._fourcc(int.from_bytes(b"RGB3", "little") | u.FOURCC_BE) == (
        "RGB3",
        True,
    )
    with pytest.raises(ValueError):
        probe._fourcc(1)
    with pytest.raises(m._EnumerationLimitReached):
        probe._call(1, 1, bytearray(), [m._MAX_IOCTLS])


def test_interval_ranges_and_nested_errors(monkeypatch: pytest.MonkeyPatch) -> None:

    probe = LinuxV4L2ModeProbe()
    responses = [u._FI.pack(0, 1, 1, 1, 3, 1, 30, 1, 15, 1, 60, 0, 0)]

    def call(fd: int, request: int, b: bytearray, budget: list[int]) -> None:
        b[:] = responses.pop()

    monkeypatch.setattr(probe, "_call", call)
    got, complete = probe._intervals(1, 1, 1, 1, [0], [0], [])
    assert complete and got[0].kind == "stepwise" and got[0].step_denominator == 60
    monkeypatch.setattr(
        probe, "_call", lambda *args: (_ for _ in ()).throw(OSError(errno.EIO, "x"))
    )
    gaps: list[str] = []
    assert probe._intervals(1, 1, 1, 1, [0], [0], gaps) == ([], False) and gaps == [
        "frame_interval_enumeration_failed"
    ]


def test_size_continuous_and_error(monkeypatch: pytest.MonkeyPatch) -> None:
    probe = LinuxV4L2ModeProbe()
    monkeypatch.setattr(
        probe,
        "_call",
        lambda fd, request, b, budget: b.__setitem__(
            slice(None), u._FS.pack(0, 1, 2, 1, 10, 0, 1, 10, 0, 0, 0)
        ),
    )
    gaps: list[str] = []
    got = probe._sizes(1, 1, [0], [0], gaps)
    assert got[0].kind == "continuous" and gaps
    monkeypatch.setattr(
        probe, "_call", lambda *args: (_ for _ in ()).throw(OSError(errno.EIO, "x"))
    )
    gaps = []
    assert probe._sizes(1, 1, [0], [0], gaps) == [] and gaps == [
        "frame_size_enumeration_failed"
    ]


def test_cli_capture_modes_uses_service(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import aurora_core.__main__ as cli

    monkeypatch.setattr(
        cli.sys,
        "argv",
        [
            "aurora",
            "hardware",
            "validate",
            "capture-modes",
            "--config",
            "configs/aurora.example.yaml",
        ],
    )
    monkeypatch.setattr(
        cli,
        "validate_capture_modes",
        lambda settings: CaptureModeValidationReport(
            ComponentId.CAPTURE_DEVICE, ComponentHealthState.UNHEALTHY, "safe", "safe"
        ),
    )
    assert cli.main() == 1
    assert "Capture mode validation: unhealthy" in capsys.readouterr().out


def test_probe_preopen_and_querycap_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    from aurora_core.hardware import capture_modes_probe as m

    monkeypatch.setattr(
        m, "_validated_video_target", lambda _: (None, "device_not_found")
    )
    assert (
        LinuxV4L2ModeProbe().enumerate_modes(identifier="x").reason_code
        == "device_not_found"
    )
    monkeypatch.setattr(m, "_validated_video_target", lambda _: ("/dev/video0", None))
    monkeypatch.setattr(
        m.os, "open", lambda p, f: (_ for _ in ()).throw(OSError(errno.EACCES, "x"))
    )
    assert (
        LinuxV4L2ModeProbe().enumerate_modes(identifier="x").reason_code
        == "permission_denied"
    )


def test_service_unsupported_platform_does_not_call_probe() -> None:
    settings = load_settings(
        environment={},
        cli_overrides={
            "capture_device": {"enabled": True, "identifier": "/dev/video0"}
        },
    )
    fake = Fake(CaptureModeProbeResult("validated"))
    assert (
        validate_capture_modes(settings, fake, platform="darwin").reason_code
        == "unsupported_platform"
        and fake.calls == 0
    )


def test_mode_printer_renders_ranges(capsys: pytest.CaptureFixture[str]) -> None:
    from aurora_core.__main__ import _print_capture_modes_report
    from aurora_core.hardware.models import (
        CaptureFrameInterval,
        CaptureFrameSize,
        CaptureModeValidationReport,
    )

    interval = CaptureFrameInterval(
        "stepwise",
        min_numerator=1,
        min_denominator=60,
        max_numerator=1,
        max_denominator=30,
        step_numerator=1,
        step_denominator=120,
    )
    size = CaptureFrameSize(
        "stepwise",
        min_width=640,
        max_width=1920,
        step_width=16,
        min_height=480,
        max_height=1080,
        step_height=8,
        intervals=(interval,),
    )
    fmt = CapturePixelFormat(
        "single_planar", "YUYV", False, None, False, False, (size,)
    )
    _print_capture_modes_report(
        CaptureModeValidationReport(
            ComponentId.CAPTURE_DEVICE,
            ComponentHealthState.DEGRADED,
            "validated_with_gaps",
            "safe",
            (fmt,),
            1,
            1,
            1,
            False,
            ("gap",),
            True,
            True,
            True,
            True,
        )
    )
    out = capsys.readouterr().out
    assert (
        "min_size: 640x480" in out
        and "size_step: 16x8" in out
        and "interval_step: 1/120 s" in out
    )


def test_probe_fstat_and_close_failure_are_sanitized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aurora_core.hardware import capture_modes_probe as m

    monkeypatch.setattr(m, "_validated_video_target", lambda _: ("/dev/video0", None))
    monkeypatch.setattr(m.os, "open", lambda p, f: 7)
    monkeypatch.setattr(m.os, "fstat", lambda fd: (_ for _ in ()).throw(OSError()))
    monkeypatch.setattr(m.os, "close", lambda fd: None)
    r = LinuxV4L2ModeProbe().enumerate_modes(identifier="x")
    assert (
        r.reason_code == "device_unavailable"
        and r.device_was_opened
        and r.descriptor_was_closed
    )
    monkeypatch.setattr(m.os, "fstat", lambda fd: SimpleNamespace(st_mode=stat.S_IFCHR))
    monkeypatch.setattr(m.os, "close", lambda fd: (_ for _ in ()).throw(OSError()))
    monkeypatch.setattr(
        m.fcntl, "ioctl", lambda *a: (_ for _ in ()).throw(OSError(errno.ENOTTY, "x"))
    )
    r = LinuxV4L2ModeProbe().enumerate_modes(identifier="x")
    assert not r.descriptor_was_closed


def test_nested_malformed_responses_retain_prior_records(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    probe = LinuxV4L2ModeProbe()
    size_calls = [
        u._FS.pack(0, 1, 1, 640, 480, 0, 0, 0, 0, 0, 0),
        u._FS.pack(9, 1, 1, 800, 600, 0, 0, 0, 0, 0, 0),
    ]
    monkeypatch.setattr(
        probe,
        "_call",
        lambda fd, request, b, budget: b.__setitem__(slice(None), size_calls.pop(0)),
    )
    monkeypatch.setattr(probe, "_intervals", lambda *args: ([], False))
    gaps: list[str] = []
    sizes = probe._sizes(1, 1, [0], [0], gaps)
    assert len(sizes) == 1 and "invalid_frame_size_response" in gaps
    probe = LinuxV4L2ModeProbe()
    int_calls = [
        u._FI.pack(0, 1, 1, 1, 1, 1, 60, 0, 0, 0, 0, 0, 0),
        u._FI.pack(9, 1, 1, 1, 1, 1, 30, 0, 0, 0, 0, 0, 0),
    ]
    monkeypatch.setattr(
        probe,
        "_call",
        lambda fd, request, b, budget: b.__setitem__(slice(None), int_calls.pop(0)),
    )
    gaps = []
    intervals, done = probe._intervals(1, 1, 1, 1, [0], [0], gaps)
    assert (
        len(intervals) == 1 and not done and "invalid_frame_interval_response" in gaps
    )


def test_ioctl_budget_exhaustion_retains_nearest_partial_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aurora_core.hardware import capture_modes_probe as m

    probe = LinuxV4L2ModeProbe()
    monkeypatch.setattr(
        probe,
        "_call",
        lambda *args: (_ for _ in ()).throw(m._EnumerationLimitReached()),
    )
    gaps: list[str] = []
    assert probe._formats(1, 1, "single_planar", [0], [0], gaps) == []
    assert gaps == ["enumeration_limit_reached"]
    gaps = []
    assert probe._sizes(1, 1, [0], [0], gaps) == [] and gaps == [
        "enumeration_limit_reached"
    ]
    gaps = []
    intervals, complete = probe._intervals(1, 1, 1, 1, [0], [0], gaps)
    assert intervals == [] and not complete and gaps == ["enumeration_limit_reached"]


def test_nested_ioctl_failures_retain_empty_partial_containers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    probe = LinuxV4L2ModeProbe()
    monkeypatch.setattr(
        probe, "_call", lambda *args: (_ for _ in ()).throw(OSError(errno.EIO, "x"))
    )
    gaps: list[str] = []
    assert probe._sizes(1, 1, [0], [0], gaps) == [] and gaps == [
        "frame_size_enumeration_failed"
    ]
    gaps = []
    values, done = probe._intervals(1, 1, 1, 1, [0], [0], gaps)
    assert values == [] and not done and gaps == ["frame_interval_enumeration_failed"]


def test_top_level_probe_failure_outcomes(monkeypatch: pytest.MonkeyPatch) -> None:
    from aurora_core.hardware import capture_modes_probe as m

    monkeypatch.setattr(m, "_validated_video_target", lambda _: ("/dev/video0", None))
    monkeypatch.setattr(m.os, "open", lambda p, f: 4)
    monkeypatch.setattr(m.os, "close", lambda fd: None)
    monkeypatch.setattr(m.os, "fstat", lambda fd: SimpleNamespace(st_mode=stat.S_IFREG))
    assert (
        LinuxV4L2ModeProbe().enumerate_modes(identifier="x").reason_code
        == "not_character_device"
    )
    monkeypatch.setattr(m.os, "fstat", lambda fd: SimpleNamespace(st_mode=stat.S_IFCHR))
    monkeypatch.setattr(
        m.fcntl,
        "ioctl",
        lambda *args: (_ for _ in ()).throw(OSError(errno.ENOTTY, "x")),
    )
    assert (
        LinuxV4L2ModeProbe().enumerate_modes(identifier="x").reason_code
        == "querycap_not_supported"
    )
    monkeypatch.setattr(
        m.fcntl, "ioctl", lambda *args: (_ for _ in ()).throw(OSError(errno.EIO, "x"))
    )
    assert (
        LinuxV4L2ModeProbe().enumerate_modes(identifier="x").reason_code
        == "capability_query_failed"
    )


def test_probe_rejects_no_capture_capability(monkeypatch: pytest.MonkeyPatch) -> None:
    from aurora_core.hardware import capture_modes_probe as m

    monkeypatch.setattr(m, "_validated_video_target", lambda _: ("/dev/video0", None))
    monkeypatch.setattr(m.os, "open", lambda p, f: 4)
    monkeypatch.setattr(m.os, "close", lambda fd: None)
    monkeypatch.setattr(m.os, "fstat", lambda fd: SimpleNamespace(st_mode=stat.S_IFCHR))
    monkeypatch.setattr(m.fcntl, "ioctl", lambda fd, request, b, mutate: 0)
    assert (
        LinuxV4L2ModeProbe().enumerate_modes(identifier="x").reason_code
        == "video_capture_not_supported"
    )


def test_mode_printer_phase_wording(capsys: pytest.CaptureFixture[str]) -> None:
    from aurora_core.__main__ import _print_capture_modes_report
    from aurora_core.hardware.models import CaptureModeValidationReport

    base = (ComponentId.CAPTURE_DEVICE, ComponentHealthState.UNHEALTHY, "safe", "safe")
    _print_capture_modes_report(CaptureModeValidationReport(*base))
    assert "No capture device was opened." in capsys.readouterr().out
    _print_capture_modes_report(
        CaptureModeValidationReport(
            *base,
            device_was_opened=True,
            querycap_was_issued=True,
            enumeration_ioctl_was_issued=True,
            descriptor_was_closed=True,
        )
    )
    assert "The capture device was closed." in capsys.readouterr().out
    _print_capture_modes_report(
        CaptureModeValidationReport(
            *base, device_was_opened=True, descriptor_was_closed=False
        )
    )
    assert "closure could not be confirmed" in capsys.readouterr().out


def test_format_response_validation_is_sanitized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    probe = LinuxV4L2ModeProbe()
    malformed = [
        u._FM.pack(
            1,
            1,
            0,
            b"bad\0" + b"\0" * 28,
            int.from_bytes(b"YUYV", "little"),
            0,
            0,
            0,
            0,
        )
    ]
    monkeypatch.setattr(
        probe,
        "_call",
        lambda fd, request, b, budget: b.__setitem__(slice(None), malformed.pop()),
    )
    with pytest.raises(ValueError):
        probe._formats(1, 1, "single_planar", [0], [0], [])
    malformed = [
        u._FM.pack(
            0, 1, 0, b"bad" + b"x" * 29, int.from_bytes(b"YUYV", "little"), 0, 0, 0, 0
        )
    ]
    monkeypatch.setattr(
        probe,
        "_call",
        lambda fd, request, b, budget: b.__setitem__(slice(None), malformed.pop()),
    )
    with pytest.raises(ValueError):
        probe._formats(1, 1, "single_planar", [0], [0], [])


def test_size_and_interval_invalid_outcomes(monkeypatch: pytest.MonkeyPatch) -> None:
    probe = LinuxV4L2ModeProbe()
    monkeypatch.setattr(
        probe, "_call", lambda *args: (_ for _ in ()).throw(OSError(errno.EINVAL, "x"))
    )
    gaps: list[str] = []
    assert probe._sizes(1, 1, [0], [0], gaps) == [] and gaps == [
        "frame_size_enumeration_unavailable"
    ]
    gaps = []
    values, done = probe._intervals(1, 1, 1, 1, [0], [0], gaps)
    assert (
        values == [] and not done and gaps == ["frame_interval_enumeration_unavailable"]
    )
    bad = [u._FS.pack(0, 1, 1, 0, 480, 0, 0, 0, 0, 0, 0)]
    monkeypatch.setattr(
        probe, "_call", lambda fd, r, b, bu: b.__setitem__(slice(None), bad.pop())
    )
    gaps = []
    assert probe._sizes(1, 1, [0], [0], gaps) == [] and gaps == [
        "invalid_frame_size_response"
    ]
    bad_i = [u._FI.pack(0, 1, 1, 1, 1, 0, 60, 0, 0, 0, 0, 0, 0)]
    monkeypatch.setattr(
        probe, "_call", lambda fd, r, b, bu: b.__setitem__(slice(None), bad_i.pop())
    )
    gaps = []
    values, done = probe._intervals(1, 1, 1, 1, [0], [0], gaps)
    assert values == [] and not done and gaps == ["invalid_frame_interval_response"]
