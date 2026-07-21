"""Tests for capture validation using fakes only; no host device is inspected."""

from __future__ import annotations

import io
import os
import stat
from types import SimpleNamespace

import pytest

from aurora_core.config import AuroraConfigurationError, load_settings
from aurora_core.hardware.capture_device import validate_capture_device
from aurora_core.hardware.capture_probe import LinuxCaptureDeviceProbe
from aurora_core.hardware.models import CaptureDeviceProbeResult
from aurora_core.runtime.models import ComponentHealthState


class FakeProbe:
    def __init__(self, result: CaptureDeviceProbeResult) -> None:
        self.result = result
        self.calls = 0

    def probe(self, *, identifier: str) -> CaptureDeviceProbeResult:
        self.calls += 1
        return self.result


@pytest.mark.parametrize(
    "identifier",
    ["/dev/video0", "/dev/video12", "/dev/v4l/by-id/camera", "/dev/v4l/by-path/port"],
)
def test_identifier_forms_are_lexically_accepted(identifier: str) -> None:
    assert (
        load_settings(
            environment={},
            cli_overrides={
                "capture_device": {"enabled": True, "identifier": identifier}
            },
        ).capture_device.identifier
        == identifier
    )


@pytest.mark.parametrize(
    "identifier",
    [
        "video0",
        "/dev/media0",
        "/dev/video0?x",
        "/dev/v4l/by-id/a/b",
        "/dev/../video0",
        "/dev/video0\n",
    ],
)
def test_unsafe_identifier_is_rejected(identifier: str) -> None:
    with pytest.raises(AuroraConfigurationError):
        load_settings(
            environment={},
            cli_overrides={
                "capture_device": {"enabled": True, "identifier": identifier}
            },
        )


def test_disabled_and_unsupported_never_call_probe() -> None:
    probe = FakeProbe(
        CaptureDeviceProbeResult("validated", True, True, True, True, "Safe camera")
    )
    assert (
        validate_capture_device(load_settings(environment={}), probe).state
        is ComponentHealthState.DISABLED
    )
    enabled = load_settings(
        environment={},
        cli_overrides={
            "capture_device": {"enabled": True, "identifier": "/dev/video0"}
        },
    )
    assert (
        validate_capture_device(enabled, probe, platform="darwin").reason_code
        == "unsupported_platform"
    )
    assert probe.calls == 0


def test_healthy_and_safe_failure_reports() -> None:
    settings = load_settings(
        environment={},
        cli_overrides={
            "capture_device": {"enabled": True, "identifier": "/dev/video0"}
        },
    )
    probe = FakeProbe(
        CaptureDeviceProbeResult("validated", True, True, True, True, "USB Video")
    )
    report = validate_capture_device(settings, probe, platform="linux")
    assert report.state is ComponentHealthState.HEALTHY and probe.calls == 1
    assert "/dev/video0" not in repr(report)
    failure = validate_capture_device(
        settings,
        FakeProbe(CaptureDeviceProbeResult("device_not_found")),
        platform="linux",
    )
    assert (
        failure.state is ComponentHealthState.UNHEALTHY
        and failure.reason_code == "device_not_found"
    )


def _install_probe_filesystem(
    monkeypatch: pytest.MonkeyPatch,
    *,
    target: str = "/dev/video12",
    target_mode: int = stat.S_IFCHR,
    name: bytes = b"USB Video\n",
    access: bool = True,
) -> list[str]:
    """Install metadata-only fakes; no fake or real video node is opened."""
    from aurora_core.hardware import capture_probe

    observed: list[str] = []
    monkeypatch.setattr(capture_probe.os, "lstat", lambda path: object())
    monkeypatch.setattr(
        capture_probe.Path, "resolve", lambda self, strict: capture_probe.Path(target)
    )

    def fake_stat(path: str | os.PathLike[str]) -> SimpleNamespace:
        observed.append(str(path))
        if str(path) == target:
            return SimpleNamespace(st_mode=target_mode)
        return SimpleNamespace(st_mode=stat.S_IFDIR)

    monkeypatch.setattr(capture_probe.os, "stat", fake_stat)
    monkeypatch.setattr(capture_probe.os, "access", lambda path, mode: access)
    monkeypatch.setattr(capture_probe.Path, "open", lambda self, mode: io.BytesIO(name))
    return observed


@pytest.mark.parametrize(
    "identifier",
    ["/dev/video12", "/dev/v4l/by-id/camera", "/dev/v4l/by-path/port"],
)
def test_production_probe_accepts_direct_and_stable_links(
    monkeypatch: pytest.MonkeyPatch, identifier: str
) -> None:
    observed = _install_probe_filesystem(monkeypatch)
    result = LinuxCaptureDeviceProbe().probe(identifier=identifier)
    assert result == CaptureDeviceProbeResult(
        "validated", True, True, True, True, "USB Video"
    )
    assert observed == ["/dev/video12", "/sys/class/video4linux/video12"]


def test_production_probe_handles_missing_and_resolution_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aurora_core.hardware import capture_probe

    monkeypatch.setattr(
        capture_probe.os,
        "lstat",
        lambda path: (_ for _ in ()).throw(FileNotFoundError()),
    )
    assert (
        LinuxCaptureDeviceProbe().probe(identifier="/dev/video0").reason_code
        == "device_not_found"
    )
    monkeypatch.setattr(capture_probe.os, "lstat", lambda path: object())
    monkeypatch.setattr(
        capture_probe.Path,
        "resolve",
        lambda self, strict: (_ for _ in ()).throw(RuntimeError()),
    )
    assert (
        LinuxCaptureDeviceProbe().probe(identifier="/dev/v4l/by-id/camera").reason_code
        == "symlink_resolution_failed"
    )


@pytest.mark.parametrize("target", ["/tmp/video0", "/dev/media0"])
def test_production_probe_rejects_unsafe_resolved_target(
    monkeypatch: pytest.MonkeyPatch, target: str
) -> None:
    _install_probe_filesystem(monkeypatch, target=target)
    result = LinuxCaptureDeviceProbe().probe(identifier="/dev/v4l/by-id/camera")
    assert result.reason_code == "invalid_device_target" and result.device_node_present


@pytest.mark.parametrize(
    "mode", [stat.S_IFREG, stat.S_IFDIR, stat.S_IFIFO, stat.S_IFSOCK]
)
def test_production_probe_rejects_non_character_targets(
    monkeypatch: pytest.MonkeyPatch, mode: int
) -> None:
    _install_probe_filesystem(monkeypatch, target_mode=mode)
    assert (
        LinuxCaptureDeviceProbe().probe(identifier="/dev/video12").reason_code
        == "not_character_device"
    )


def test_production_probe_handles_sysfs_and_access_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aurora_core.hardware import capture_probe

    _install_probe_filesystem(monkeypatch)
    real_stat = capture_probe.os.stat
    monkeypatch.setattr(
        capture_probe.os,
        "stat",
        lambda path: (
            (_ for _ in ()).throw(FileNotFoundError())
            if "video4linux" in str(path)
            else real_stat(path)
        ),
    )
    assert (
        LinuxCaptureDeviceProbe().probe(identifier="/dev/video12").reason_code
        == "v4l2_registration_missing"
    )
    _install_probe_filesystem(monkeypatch, access=False)
    assert (
        LinuxCaptureDeviceProbe().probe(identifier="/dev/video12").reason_code
        == "permission_denied"
    )


def test_production_probe_safely_handles_target_and_sysfs_stat_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aurora_core.hardware import capture_probe

    _install_probe_filesystem(monkeypatch)
    monkeypatch.setattr(
        capture_probe.os,
        "stat",
        lambda path: (
            (_ for _ in ()).throw(OSError())
            if str(path) == "/dev/video12"
            else SimpleNamespace(st_mode=stat.S_IFDIR)
        ),
    )
    assert (
        LinuxCaptureDeviceProbe().probe(identifier="/dev/video12").reason_code
        == "probe_failed"
    )
    _install_probe_filesystem(monkeypatch)
    original_stat = capture_probe.os.stat
    monkeypatch.setattr(
        capture_probe.os,
        "stat",
        lambda path: (
            (_ for _ in ()).throw(OSError())
            if "video4linux" in str(path)
            else original_stat(path)
        ),
    )
    assert (
        LinuxCaptureDeviceProbe().probe(identifier="/dev/video12").reason_code
        == "metadata_unavailable"
    )


@pytest.mark.parametrize(
    "raw, expected",
    [
        (b"", None),
        (b"bad\xff", None),
        (b"bad\x01", None),
        (b"x" * 129, None),
        (b"USB Video\r\n", "USB Video"),
    ],
)
def test_name_reader_sanitizes_bounded_sysfs_metadata(
    raw: bytes, expected: str | None
) -> None:
    class Attribute(io.BytesIO):
        amount: int | None = None

        def read(self, size: int = -1) -> bytes:
            self.amount = size
            return super().read(size)

    attribute = Attribute(raw)
    from aurora_core.hardware import capture_probe

    original_open = capture_probe.Path.open
    try:
        capture_probe.Path.open = lambda self, mode: attribute  # type: ignore[method-assign]
        assert (
            LinuxCaptureDeviceProbe._read_name(capture_probe.Path("/fake/name"))
            == expected
        )
    finally:
        capture_probe.Path.open = original_open  # type: ignore[method-assign]
    assert attribute.amount == 256


def test_name_reader_rejects_full_bounded_buffer_and_open_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aurora_core.hardware import capture_probe

    _install_probe_filesystem(monkeypatch, name=b"x" * 256)
    assert (
        LinuxCaptureDeviceProbe().probe(identifier="/dev/video12").reason_code
        == "invalid_device_name"
    )
    monkeypatch.setattr(
        capture_probe.Path,
        "open",
        lambda self, mode: (_ for _ in ()).throw(OSError()),
    )
    assert LinuxCaptureDeviceProbe._read_name(capture_probe.Path("/fake/name")) is None


def test_production_probe_safely_handles_unexpected_os_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aurora_core.hardware import capture_probe

    monkeypatch.setattr(
        capture_probe.os, "lstat", lambda path: (_ for _ in ()).throw(OSError())
    )
    assert (
        LinuxCaptureDeviceProbe().probe(identifier="/dev/video0").reason_code
        == "probe_failed"
    )


def test_capture_validation_service_safely_maps_probe_exception() -> None:
    class RaisingProbe:
        def probe(self, *, identifier: str) -> CaptureDeviceProbeResult:
            raise OSError("private path must not escape")

    settings = load_settings(
        environment={},
        cli_overrides={
            "capture_device": {"enabled": True, "identifier": "/dev/video0"}
        },
    )
    assert (
        validate_capture_device(settings, RaisingProbe(), platform="linux").reason_code
        == "probe_failed"
    )


def test_capture_cli_healthy_output_is_sanitized(monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    from aurora_core.__main__ import main
    from aurora_core.hardware.models import CaptureDeviceValidationReport
    from aurora_core.runtime.models import ComponentId

    report = CaptureDeviceValidationReport(
        ComponentId.CAPTURE_DEVICE,
        ComponentHealthState.HEALTHY,
        "validated",
        "safe",
        True,
        True,
        True,
        True,
        "USB Video",
    )
    monkeypatch.setattr(
        "aurora_core.__main__.validate_capture_device", lambda settings: report
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "aurora",
            "hardware",
            "validate",
            "capture-device",
            "--identifier",
            "/dev/video9",
        ],
    )
    assert main() == 0
    output = capsys.readouterr().out
    assert "device_name: USB Video" in output
    assert "/dev/video9" not in output and "not opened" in output


def test_capture_cli_disabled_and_invalid_configuration(monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    from aurora_core.__main__ import main

    monkeypatch.setattr(
        "sys.argv", ["aurora", "hardware", "validate", "capture-device"]
    )
    assert main() == 1
    assert "No capture-device probe was performed." in capsys.readouterr().out
    monkeypatch.setattr(
        "sys.argv",
        ["aurora", "hardware", "validate", "capture-device", "--identifier", "bad"],
    )
    assert main() == 1
    assert "bad" not in capsys.readouterr().err
