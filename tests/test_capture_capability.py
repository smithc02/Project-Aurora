"""Synthetic tests for the one-shot V4L2 capability-query boundary."""

from __future__ import annotations

import errno
import os
import stat
import struct
from types import SimpleNamespace

import pytest

from aurora_core.config import load_settings
from aurora_core.hardware.capture_capability import validate_capture_capability
from aurora_core.hardware.capture_capability_probe import (
    _CAPABILITY_SIZE,
    _V4L2_CAP_DEVICE_CAPS,
    _V4L2_CAP_READWRITE,
    _V4L2_CAP_STREAMING,
    _V4L2_CAP_VIDEO_CAPTURE,
    _V4L2_CAP_VIDEO_CAPTURE_MPLANE,
    _VIDIOC_QUERYCAP,
    LinuxV4L2CapabilityProbe,
)
from aurora_core.hardware.models import CaptureCapabilityProbeResult
from aurora_core.runtime.models import ComponentHealthState


def _response(
    *,
    capabilities: int,
    device_caps: int = 0,
    driver: bytes = b"uvcvideo",
    card: bytes = b"USB Video",
    reserved: int = 0,
) -> bytes:
    return struct.pack(
        "<16s32s32sIIIIII",
        driver,
        card,
        b"private-bus",
        0x06120A,
        capabilities,
        device_caps,
        reserved,
        0,
        0,
    )


def _install_os(
    monkeypatch: pytest.MonkeyPatch, response: bytes | None = None
) -> dict[str, object]:
    from aurora_core.hardware import capture_capability_probe as module

    calls: dict[str, object] = {"open": 0, "close": [], "ioctl": []}
    monkeypatch.setattr(module.os, "lstat", lambda path: object())
    monkeypatch.setattr(
        module.Path, "resolve", lambda self, strict: module.Path("/dev/video12")
    )
    monkeypatch.setattr(
        module.os, "stat", lambda path: SimpleNamespace(st_mode=stat.S_IFCHR)
    )

    def open_(path: str, flags: int) -> int:
        calls["open"] = int(calls["open"]) + 1
        calls["flags"] = flags
        return 47

    monkeypatch.setattr(module.os, "open", open_)
    monkeypatch.setattr(
        module.os, "fstat", lambda fd: SimpleNamespace(st_mode=stat.S_IFCHR)
    )
    monkeypatch.setattr(module.os, "close", lambda fd: calls["close"].append(fd))

    def ioctl(fd: int, request: int, buffer: bytearray, mutate: bool) -> int:
        calls["ioctl"].append((fd, request, bytes(buffer), mutate))
        assert response is not None
        buffer[:] = response
        return 0

    monkeypatch.setattr(module.fcntl, "ioctl", ioctl)
    return calls


def test_structure_and_request_are_linux_uapi_values() -> None:
    assert _CAPABILITY_SIZE == 104
    assert _VIDIOC_QUERYCAP == 0x80685600


def test_probe_uses_one_zeroed_buffer_open_ioctl_and_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _install_os(
        monkeypatch,
        _response(capabilities=_V4L2_CAP_VIDEO_CAPTURE | _V4L2_CAP_STREAMING),
    )
    result = LinuxV4L2CapabilityProbe().query(identifier="/dev/video0")
    assert result.query_succeeded and result.driver_name == "uvcvideo"
    assert result.card_name == "USB Video" and result.v4l2_api_version == "6.18.10"
    assert calls["open"] == 1 and calls["close"] == [47]
    fd, request, buffer, mutate = calls["ioctl"][0]
    assert (fd, request, mutate) == (47, _VIDIOC_QUERYCAP, True)
    assert buffer == bytes(104)
    flags = int(calls["flags"])
    assert flags & os.O_RDWR and flags & os.O_NONBLOCK
    assert flags & getattr(os, "O_CLOEXEC", 0) == getattr(os, "O_CLOEXEC", 0)
    assert not flags & getattr(os, "O_CREAT", 0)


@pytest.mark.parametrize(
    "driver, card",
    [
        (b"x" * 16, b"USB Video"),
        (b"good", b"x" * 32),
        (b"bad\x01", b"USB Video"),
        (b"good", b"bad\xff"),
    ],
)
def test_invalid_fixed_strings_are_sanitized(
    monkeypatch: pytest.MonkeyPatch, driver: bytes, card: bytes
) -> None:
    _install_os(
        monkeypatch,
        _response(
            capabilities=_V4L2_CAP_VIDEO_CAPTURE | _V4L2_CAP_STREAMING,
            driver=driver,
            card=card,
        ),
    )
    result = LinuxV4L2CapabilityProbe().query(identifier="/dev/video0")
    assert result.reason_code == "invalid_capability_response"
    assert "bad" not in repr(result)


def test_device_caps_overrides_whole_device_capabilities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_os(
        monkeypatch,
        _response(
            capabilities=_V4L2_CAP_DEVICE_CAPS | _V4L2_CAP_VIDEO_CAPTURE,
            device_caps=_V4L2_CAP_READWRITE,
        ),
    )
    result = LinuxV4L2CapabilityProbe().query(identifier="/dev/video0")
    assert not result.single_planar_capture and result.readwrite_io


def test_multiplanar_and_both_io_methods_are_recognized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_os(
        monkeypatch,
        _response(
            capabilities=_V4L2_CAP_VIDEO_CAPTURE_MPLANE
            | _V4L2_CAP_STREAMING
            | _V4L2_CAP_READWRITE
        ),
    )
    result = LinuxV4L2CapabilityProbe().query(identifier="/dev/video0")
    assert result.multi_planar_capture and result.streaming_io and result.readwrite_io


def test_close_occurs_after_ioctl_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _install_os(monkeypatch, _response(capabilities=0))
    from aurora_core.hardware import capture_capability_probe as module

    monkeypatch.setattr(
        module.fcntl,
        "ioctl",
        lambda *args: (_ for _ in ()).throw(OSError(errno.ENOTTY, "private")),
    )
    assert (
        LinuxV4L2CapabilityProbe().query(identifier="/dev/video0").reason_code
        == "querycap_not_supported"
    )
    assert calls["close"] == [47]


def test_open_failure_does_not_close_or_ioctl(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _install_os(monkeypatch, _response(capabilities=0))
    from aurora_core.hardware import capture_capability_probe as module

    monkeypatch.setattr(
        module.os,
        "open",
        lambda path, flags: (_ for _ in ()).throw(OSError(errno.EACCES, "secret")),
    )
    assert (
        LinuxV4L2CapabilityProbe().query(identifier="/dev/video0").reason_code
        == "permission_denied"
    )
    assert calls["close"] == [] and calls["ioctl"] == []


class _FakeProbe:
    def __init__(self, result: CaptureCapabilityProbeResult) -> None:
        self.result = result
        self.calls = 0

    def query(self, *, identifier: str) -> CaptureCapabilityProbeResult:
        self.calls += 1
        return self.result


def test_service_maps_health_without_hardware() -> None:
    disabled = _FakeProbe(CaptureCapabilityProbeResult("validated"))
    assert (
        validate_capture_capability(load_settings(environment={}), disabled).state
        is ComponentHealthState.DISABLED
    )
    settings = load_settings(
        environment={},
        cli_overrides={
            "capture_device": {"enabled": True, "identifier": "/dev/video0"}
        },
    )
    good = _FakeProbe(
        CaptureCapabilityProbeResult(
            "validated",
            True,
            "uvcvideo",
            "USB Video",
            "6.12.0",
            True,
            False,
            True,
            False,
        )
    )
    assert (
        validate_capture_capability(settings, good, platform="linux").state
        is ComponentHealthState.HEALTHY
    )
    missing_capture = _FakeProbe(
        CaptureCapabilityProbeResult(
            "validated",
            True,
            "uvcvideo",
            "USB Video",
            "6.12.0",
            False,
            False,
            True,
            False,
        )
    )
    assert (
        validate_capture_capability(
            settings, missing_capture, platform="linux"
        ).reason_code
        == "video_capture_not_supported"
    )
    assert disabled.calls == 0 and good.calls == 1


def test_cli_disabled_does_not_call_probe(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from aurora_core.__main__ import main

    monkeypatch.setattr(
        "sys.argv",
        [
            "aurora",
            "hardware",
            "validate",
            "capture-capability",
            "--config",
            "configs/aurora.example.yaml",
        ],
    )
    assert main() == 1
    output = capsys.readouterr().out
    assert "No capture device was opened." in output and "/dev/" not in output


@pytest.mark.parametrize(
    ("error_number", "expected"),
    [
        (errno.EPERM, "permission_denied"),
        (errno.EBUSY, "device_busy"),
        (errno.ENODEV, "device_unavailable"),
        (errno.EIO, "device_open_failed"),
    ],
)
def test_open_errors_are_safe(
    monkeypatch: pytest.MonkeyPatch, error_number: int, expected: str
) -> None:
    _install_os(monkeypatch, _response(capabilities=0))
    from aurora_core.hardware import capture_capability_probe as module

    monkeypatch.setattr(
        module.os,
        "open",
        lambda path, flags: (_ for _ in ()).throw(OSError(error_number, "secret")),
    )
    assert (
        LinuxV4L2CapabilityProbe().query(identifier="/dev/video0").reason_code
        == expected
    )


def test_reserved_and_fstat_failures_close(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _install_os(
        monkeypatch,
        _response(
            capabilities=_V4L2_CAP_VIDEO_CAPTURE | _V4L2_CAP_STREAMING, reserved=1
        ),
    )
    assert (
        LinuxV4L2CapabilityProbe().query(identifier="/dev/video0").reason_code
        == "invalid_capability_response"
    )
    assert calls["close"] == [47]


def test_fstat_and_other_ioctl_errors_are_safe(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _install_os(monkeypatch, _response(capabilities=0))
    from aurora_core.hardware import capture_capability_probe as module

    monkeypatch.setattr(
        module.os,
        "fstat",
        lambda fd: (_ for _ in ()).throw(OSError(errno.EIO, "secret")),
    )
    assert (
        LinuxV4L2CapabilityProbe().query(identifier="/dev/video0").reason_code
        == "device_unavailable"
    )
    assert calls["close"] == [47]
    calls = _install_os(monkeypatch, _response(capabilities=0))
    monkeypatch.setattr(
        module.fcntl,
        "ioctl",
        lambda *args: (_ for _ in ()).throw(OSError(errno.EIO, "secret")),
    )
    assert (
        LinuxV4L2CapabilityProbe().query(identifier="/dev/video0").reason_code
        == "capability_query_failed"
    )
    assert calls["close"] == [47]
    calls = _install_os(monkeypatch, _response(capabilities=0))
    from aurora_core.hardware import capture_capability_probe as module

    monkeypatch.setattr(
        module.os, "fstat", lambda fd: SimpleNamespace(st_mode=stat.S_IFREG)
    )
    assert (
        LinuxV4L2CapabilityProbe().query(identifier="/dev/video0").reason_code
        == "not_character_device"
    )
    assert calls["close"] == [47]


def test_cli_healthy_output_is_sanitized(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from aurora_core.__main__ import main
    from aurora_core.hardware.models import CaptureCapabilityValidationReport
    from aurora_core.runtime.models import ComponentId

    report = CaptureCapabilityValidationReport(
        ComponentId.CAPTURE_DEVICE,
        ComponentHealthState.HEALTHY,
        "validated",
        "safe",
        True,
        "uvcvideo",
        "USB Video",
        "6.12.0",
        True,
        False,
        True,
        False,
    )
    monkeypatch.setattr(
        "aurora_core.__main__.validate_capture_capability", lambda settings: report
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "aurora",
            "hardware",
            "validate",
            "capture-capability",
            "--identifier",
            "/dev/video9",
        ],
    )
    assert main() == 0
    output = capsys.readouterr().out
    assert "driver: uvcvideo" in output and "then closed" in output
    assert "/dev/video9" not in output


def test_service_unsupported_exception_and_missing_io_are_safe() -> None:
    settings = load_settings(
        environment={},
        cli_overrides={
            "capture_device": {"enabled": True, "identifier": "/dev/video0"}
        },
    )
    good = CaptureCapabilityProbeResult(
        "validated", True, "uvcvideo", "USB Video", "6.12.0", True, False, False, False
    )
    assert (
        validate_capture_capability(
            settings, _FakeProbe(good), platform="linux"
        ).reason_code
        == "capture_io_method_missing"
    )
    assert (
        validate_capture_capability(
            settings, _FakeProbe(good), platform="darwin"
        ).reason_code
        == "unsupported_platform"
    )

    class RaisingProbe:
        def query(self, *, identifier: str) -> CaptureCapabilityProbeResult:
            raise RuntimeError("secret")

    assert (
        validate_capture_capability(
            settings, RaisingProbe(), platform="linux"
        ).reason_code
        == "capability_query_failed"
    )
