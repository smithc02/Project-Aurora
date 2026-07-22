"""Synthetic safety checks for bounded capture-frame validation."""

from __future__ import annotations

import errno
import os
import select
import stat
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from aurora_core.__main__ import _print_capture_frame_report, main
from aurora_core.config.models import AuroraSettings, CaptureDeviceSettings
from aurora_core.hardware import capture_frame_probe as probe_module
from aurora_core.hardware.capture_frame import validate_capture_frame
from aurora_core.hardware.capture_frame_probe import (
    LinuxV4L2CaptureFrameProbe,
    _capabilities_valid,
    _format_valid,
    _supported_abi,
    _validated_target,
    read_frame_into,
)
from aurora_core.hardware.models import (
    CaptureFrameProbeResult,
    CaptureFrameValidationReport,
)
from aurora_core.hardware.v4l2_uapi import (
    CAP_DEVICE_CAPS,
    CAP_READWRITE,
    CAP_VIDEO_CAPTURE,
    VIDIOC_G_FMT,
    VIDIOC_QUERYCAP,
    capture_format_request,
)
from aurora_core.runtime.models import ComponentHealthState, ComponentId

_FD = 41
_SIZEIMAGE = 8
_SETTINGS = AuroraSettings(
    capture_device=CaptureDeviceSettings(enabled=True, identifier="/dev/video0")
)


class _Poll:
    def __init__(
        self,
        events: list[tuple[int, int]] | None = None,
        failures: list[BaseException] | None = None,
    ) -> None:
        self.events = events if events is not None else [(_FD, select.POLLIN)]
        self.failures = list(failures or [])
        self.registrations: list[tuple[int, int]] = []
        self.timeouts: list[int] = []

    def register(self, fd: int, mask: int) -> None:
        self.registrations.append((fd, mask))

    def poll(self, timeout: int) -> list[tuple[int, int]]:
        self.timeouts.append(timeout)
        if self.failures:
            raise self.failures.pop(0)
        return self.events


class _Clock:
    def __init__(self, *values: float) -> None:
        self.values = list(values)
        self.last = values[-1] if values else 0.0

    def __call__(self) -> float:
        if self.values:
            self.last = self.values.pop(0)
        return self.last


def _good_ioctl(fd: int, request: int, buffer: bytearray, mutate: bool) -> int:
    assert fd == _FD and mutate
    if request == VIDIOC_QUERYCAP:
        assert len(buffer) == 104
        buffer[84:88] = (CAP_VIDEO_CAPTURE | CAP_READWRITE).to_bytes(4, "little")
    else:
        assert request == VIDIOC_G_FMT and len(buffer) == 208
        buffer[8:12] = (640).to_bytes(4, "little")
        buffer[12:16] = (480).to_bytes(4, "little")
        buffer[28:32] = _SIZEIMAGE.to_bytes(4, "little")
    return 0


def _reader(fd: int, buffer: bytearray) -> int:
    assert fd == _FD and isinstance(buffer, bytearray)
    buffer[:4] = b"data"
    return 4


def _make_probe(**overrides: Any) -> LinuxV4L2CaptureFrameProbe:
    defaults: dict[str, Any] = {
        "abi_supported": lambda: True,
        "target_validator": lambda _identifier: ("synthetic", None),
        "opener": lambda _target, _flags: _FD,
        "fstat": lambda _fd: SimpleNamespace(st_mode=stat.S_IFCHR),
        "ioctl": _good_ioctl,
        "poll_factory": _Poll,
        "reader": _reader,
        "closer": lambda _fd: None,
        "monotonic": lambda: 0.0,
    }
    defaults.update(overrides)
    return LinuxV4L2CaptureFrameProbe(**defaults)


def _valid_result(**changes: Any) -> CaptureFrameProbeResult:
    result = CaptureFrameProbeResult(
        reason_code="validated",
        device_was_opened=True,
        descriptor_was_closed=True,
        capability_query_succeeded=True,
        current_format_query_succeeded=True,
        acquisition_method="readwrite",
        poll_was_attempted=True,
        frame_read_was_attempted=True,
        frame_received=True,
        frame_byte_count=4,
        current_width=640,
        current_height=480,
        current_sizeimage=8,
        frame_buffer_wipe_completed=True,
        cleanup_completed=True,
        streaming_io_was_used=False,
    )
    return replace(result, **changes)


class _FakeProbe:
    def __init__(
        self,
        result: CaptureFrameProbeResult | None = None,
        error: Exception | None = None,
    ) -> None:
        self.result = result or CaptureFrameProbeResult("unexpected_probe_failure")
        self.error = error
        self.calls = 0

    def probe(self, *, identifier: str) -> CaptureFrameProbeResult:
        assert identifier == "/dev/video0"
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.result


def _report(
    state: ComponentHealthState, **changes: Any
) -> CaptureFrameValidationReport:
    report = CaptureFrameValidationReport(
        component_id=ComponentId.CAPTURE_DEVICE,
        state=state,
        reason_code="validated" if state is ComponentHealthState.HEALTHY else "failed",
        message="private-message-canary",
    )
    return replace(report, **changes)


def test_disabled_performs_no_probe() -> None:
    fake = _FakeProbe(_valid_result())
    report = validate_capture_frame(
        AuroraSettings(capture_device=CaptureDeviceSettings(enabled=False)), fake
    )
    assert report.state is ComponentHealthState.DISABLED
    assert report.reason_code == "capture_device_disabled" and fake.calls == 0


def test_unsupported_platform_performs_no_probe() -> None:
    fake = _FakeProbe(_valid_result())
    report = validate_capture_frame(_SETTINGS, fake, platform="darwin")
    assert report.state is ComponentHealthState.UNHEALTHY
    assert report.reason_code == "unsupported_platform" and fake.calls == 0


@pytest.mark.parametrize(
    "platform, byteorder, pointer_size, expected",
    [
        ("darwin", "little", 8, False),
        ("linux", "little", 4, False),
        ("linux", "big", 8, False),
        ("linux", "little", 8, True),
    ],
)
def test_abi_gate_checks_platform_pointer_width_and_endianness(
    monkeypatch: pytest.MonkeyPatch,
    platform: str,
    byteorder: str,
    pointer_size: int,
    expected: bool,
) -> None:
    monkeypatch.setattr(probe_module.sys, "platform", platform)
    monkeypatch.setattr(probe_module.sys, "byteorder", byteorder)
    monkeypatch.setattr(probe_module, "struct_pointer_size", lambda: pointer_size)
    assert _supported_abi() is expected


def test_unsupported_abi_stops_before_target_validation() -> None:
    def forbidden(_identifier: str) -> tuple[str | None, str | None]:
        raise AssertionError("target validation must not run")

    result = LinuxV4L2CaptureFrameProbe(
        abi_supported=lambda: False, target_validator=forbidden
    ).probe(identifier="private-target")
    assert result == CaptureFrameProbeResult("unsupported_abi")


@pytest.mark.parametrize(
    "failure, expected",
    [
        (FileNotFoundError(), "device_not_found"),
        (OSError(), "symlink_resolution_failed"),
        (RuntimeError(), "symlink_resolution_failed"),
    ],
)
def test_target_resolution_failures_are_sanitized(
    monkeypatch: pytest.MonkeyPatch, failure: Exception, expected: str
) -> None:
    def fail_resolve(self: Path, *, strict: bool) -> Path:
        assert strict
        raise failure

    monkeypatch.setattr(probe_module.Path, "resolve", fail_resolve)
    assert _validated_target("private-target") == (None, expected)


def test_target_rejects_resolved_non_video_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        probe_module.Path,
        "resolve",
        lambda self, strict: probe_module.Path("/private/device-canary"),
    )
    assert _validated_target("private-target") == (None, "invalid_device_target")


@pytest.mark.parametrize(
    "stat_outcome, expected",
    [
        (FileNotFoundError(), "device_not_found"),
        (OSError(), "device_unavailable"),
        (SimpleNamespace(st_mode=stat.S_IFREG), "not_character_device"),
        (SimpleNamespace(st_mode=stat.S_IFCHR), None),
    ],
)
def test_resolved_target_validation(
    monkeypatch: pytest.MonkeyPatch, stat_outcome: object, expected: str | None
) -> None:
    monkeypatch.setattr(
        probe_module.Path,
        "resolve",
        lambda self, strict: probe_module.Path("/dev/video19"),
    )

    def synthetic_stat(_path: str) -> object:
        if isinstance(stat_outcome, Exception):
            raise stat_outcome
        return stat_outcome

    monkeypatch.setattr(probe_module.os, "stat", synthetic_stat)
    target, reason = _validated_target("private-target")
    assert reason == expected
    assert target == ("/dev/video19" if expected is None else None)


@pytest.mark.parametrize(
    "error_number, expected",
    [
        (errno.EACCES, "permission_denied"),
        (errno.EPERM, "permission_denied"),
        (errno.EBUSY, "device_busy"),
        (errno.ENOENT, "device_unavailable"),
        (errno.ENODEV, "device_unavailable"),
        (errno.ENXIO, "device_unavailable"),
        (errno.EIO, "device_open_failed"),
    ],
)
def test_open_error_mappings(error_number: int, expected: str) -> None:
    def fail_open(_target: str, _flags: int) -> int:
        raise OSError(error_number, "private errno text")

    result = _make_probe(opener=fail_open).probe(identifier="private-target")
    assert result.reason_code == expected
    assert "private" not in repr(result) and not result.device_was_opened


def test_open_uses_nonblocking_readwrite_flags_and_no_create() -> None:
    seen: list[tuple[str, int]] = []

    def opener(target: str, flags: int) -> int:
        seen.append((target, flags))
        return _FD

    assert _make_probe(opener=opener).probe(identifier="synthetic").frame_received
    target, flags = seen[0]
    assert target == "synthetic" and flags & os.O_RDWR and flags & os.O_NONBLOCK
    assert not flags & getattr(os, "O_CREAT", 0)


@pytest.mark.parametrize(
    "failure, expected",
    [
        (OSError(errno.EIO, "private"), "descriptor_stat_failed"),
        (ValueError("private"), "unexpected_probe_failure"),
    ],
)
def test_only_fstat_oserror_maps_to_descriptor_stat_failed(
    failure: Exception, expected: str
) -> None:
    closed: list[int] = []

    def fail_stat(_fd: int) -> object:
        raise failure

    result = _make_probe(fstat=fail_stat, closer=closed.append).probe(identifier="x")
    assert result.reason_code == expected
    assert result.descriptor_was_closed and closed == [_FD]


def test_opened_non_character_descriptor_is_rejected_and_closed() -> None:
    result = _make_probe(fstat=lambda _fd: SimpleNamespace(st_mode=stat.S_IFREG)).probe(
        identifier="x"
    )
    assert result.reason_code == "not_character_device"
    assert result.device_was_opened and result.cleanup_completed


def test_capability_parser_ignores_strings_and_uses_whole_device_caps() -> None:
    response = bytearray(104)
    response[:80] = (b"driver-card-private-canary" * 4)[:80]
    response[84:88] = (CAP_VIDEO_CAPTURE | CAP_READWRITE).to_bytes(4, "little")
    assert _capabilities_valid(response) == (True, True)


def test_capability_parser_uses_effective_device_caps() -> None:
    response = bytearray(104)
    response[84:88] = (CAP_DEVICE_CAPS | CAP_VIDEO_CAPTURE).to_bytes(4, "little")
    response[88:92] = CAP_READWRITE.to_bytes(4, "little")
    assert _capabilities_valid(response) == (False, True)


@pytest.mark.parametrize(
    "response",
    [bytearray(103), bytearray(104)[:92] + bytearray(b"\x01") + bytearray(11)],
)
def test_invalid_capability_response_shape(response: bytearray) -> None:
    assert _capabilities_valid(response) == "invalid"


@pytest.mark.parametrize(
    "caps, expected",
    [
        (CAP_READWRITE, "single_planar_capture_not_supported"),
        (CAP_VIDEO_CAPTURE, "readwrite_io_not_supported"),
    ],
)
def test_missing_required_capabilities_stop_before_g_fmt(
    caps: int, expected: str
) -> None:
    requests: list[int] = []

    def ioctl(fd: int, request: int, buffer: bytearray, mutate: bool) -> int:
        requests.append(request)
        buffer[84:88] = caps.to_bytes(4, "little")
        return 0

    result = _make_probe(ioctl=ioctl).probe(identifier="x")
    assert result.reason_code == expected and requests == [VIDIOC_QUERYCAP]


@pytest.mark.parametrize(
    "stage, error_number, expected",
    [
        ("query", errno.ENOTTY, "querycap_not_supported"),
        ("query", errno.EINVAL, "querycap_not_supported"),
        ("query", errno.EIO, "capability_query_failed"),
        ("format", errno.ENOTTY, "current_format_not_supported"),
        ("format", errno.EINVAL, "current_format_not_supported"),
        ("format", errno.EIO, "current_format_query_failed"),
    ],
)
def test_ioctl_error_mappings(stage: str, error_number: int, expected: str) -> None:
    def ioctl(fd: int, request: int, buffer: bytearray, mutate: bool) -> int:
        if stage == "query" or request == VIDIOC_G_FMT:
            raise OSError(error_number, "private")
        return _good_ioctl(fd, request, buffer, mutate)

    result = _make_probe(ioctl=ioctl).probe(identifier="x")
    assert result.reason_code == expected and "private" not in repr(result)


def test_querycap_and_g_fmt_share_one_four_call_budget() -> None:
    requests: list[int] = []

    def ioctl(fd: int, request: int, buffer: bytearray, mutate: bool) -> int:
        requests.append(request)
        if len(requests) <= 2:
            raise OSError(errno.EINTR, "private")
        return _good_ioctl(fd, request, buffer, mutate)

    result = _make_probe(ioctl=ioctl).probe(identifier="x")
    assert requests == [VIDIOC_QUERYCAP] * 3 + [VIDIOC_G_FMT]
    assert len(requests) == 4
    assert result.reason_code == "validated"
    assert result.capability_query_succeeded and result.current_format_query_succeeded
    assert result.acquisition_method == "readwrite"
    assert result.poll_was_attempted and result.frame_read_was_attempted
    assert result.frame_received and result.cleanup_completed


def test_no_fifth_ioctl_after_querycap_exhausts_budget() -> None:
    requests: list[int] = []

    def ioctl(fd: int, request: int, buffer: bytearray, mutate: bool) -> int:
        requests.append(request)
        if len(requests) < 4:
            raise OSError(errno.EINTR, "private")
        return _good_ioctl(fd, request, buffer, mutate)

    result = _make_probe(ioctl=ioctl).probe(identifier="x")
    assert requests == [VIDIOC_QUERYCAP] * 4
    assert result.reason_code == "capability_ioctl_interrupted_budget_exhausted"
    assert (
        result.capability_query_succeeded and not result.current_format_query_succeeded
    )


def test_ioctl_eintr_exhaustion_never_makes_a_fifth_call() -> None:
    calls = 0

    def interrupted(*_args: object) -> int:
        nonlocal calls
        calls += 1
        raise OSError(errno.EINTR, "private")

    result = _make_probe(ioctl=interrupted).probe(identifier="x")
    assert calls == 4
    assert result.reason_code == "capability_ioctl_interrupted_budget_exhausted"


@pytest.mark.parametrize(
    "clock_values, expected_requests",
    [
        ((0.0, 2.0), []),
        ((0.0, 0.0, 2.0), [VIDIOC_QUERYCAP]),
    ],
)
def test_deadline_expiration_before_each_ioctl_stage(
    clock_values: tuple[float, ...], expected_requests: list[int]
) -> None:
    requests: list[int] = []

    def ioctl(fd: int, request: int, buffer: bytearray, mutate: bool) -> int:
        requests.append(request)
        return _good_ioctl(fd, request, buffer, mutate)

    result = _make_probe(ioctl=ioctl, monotonic=_Clock(*clock_values)).probe(
        identifier="x"
    )
    assert result.reason_code == "validation_deadline_exceeded"
    assert requests == expected_requests and result.cleanup_completed


@pytest.mark.parametrize(
    "interrupted_request, clock_values, expected_requests, expected_reason",
    [
        (
            VIDIOC_QUERYCAP,
            (0.0, 0.0, 2.0),
            [VIDIOC_QUERYCAP],
            "capability_ioctl_interrupted_budget_exhausted",
        ),
        (
            VIDIOC_G_FMT,
            (0.0, 0.0, 0.0, 2.0),
            [VIDIOC_QUERYCAP, VIDIOC_G_FMT],
            "current_format_ioctl_interrupted_budget_exhausted",
        ),
    ],
)
def test_ioctl_eintr_retry_requires_remaining_deadline(
    interrupted_request: int,
    clock_values: tuple[float, ...],
    expected_requests: list[int],
    expected_reason: str,
) -> None:
    requests: list[int] = []

    def ioctl(fd: int, request: int, buffer: bytearray, mutate: bool) -> int:
        requests.append(request)
        if request == interrupted_request:
            raise OSError(errno.EINTR, "private")
        return _good_ioctl(fd, request, buffer, mutate)

    result = _make_probe(ioctl=ioctl, monotonic=_Clock(*clock_values)).probe(
        identifier="x"
    )
    assert result.reason_code == expected_reason
    assert requests == expected_requests


def test_deadline_before_poll_skips_poll_and_still_wipes_and_closes() -> None:
    poller = _Poll()
    closed: list[int] = []
    result = _make_probe(
        poll_factory=lambda: poller,
        closer=closed.append,
        monotonic=_Clock(0.0, 0.0, 0.0, 2.0),
    ).probe(identifier="x")
    assert result.reason_code == "validation_deadline_exceeded"
    assert not poller.timeouts and not result.poll_was_attempted
    assert result.frame_buffer_wipe_completed and result.cleanup_completed
    assert closed == [_FD]


def test_deadline_before_read_skips_first_read_and_runs_cleanup() -> None:
    reads = 0

    def reader(_fd: int, _buffer: bytearray) -> int:
        nonlocal reads
        reads += 1
        return 1

    result = _make_probe(
        reader=reader,
        monotonic=_Clock(0.0, 0.0, 0.0, 0.0, 0.0, 2.0),
    ).probe(identifier="x")
    assert result.reason_code == "validation_deadline_exceeded" and reads == 0
    assert result.poll_was_attempted and not result.frame_read_was_attempted
    assert result.frame_buffer_wipe_completed and result.cleanup_completed


def test_small_positive_poll_duration_is_rounded_up_not_truncated() -> None:
    poller = _Poll(events=[])
    result = _make_probe(
        poll_factory=lambda: poller,
        monotonic=_Clock(0.0, 0.0, 0.0, 0.0, 1.9999),
    ).probe(identifier="x")
    assert result.reason_code == "poll_timeout" and poller.timeouts == [1]


def test_uapi_g_fmt_request_is_exactly_208_bytes_and_zeroed() -> None:
    request = capture_format_request()
    assert len(request) == 208
    assert request[:4] == b"\x01\0\0\0"
    assert request[4:] == b"\0" * 204


@pytest.mark.parametrize(
    "width, height, sizeimage, expected",
    [
        (1, 1, 1, (1, 1, 1)),
        (8192, 8192, 8 * 1024 * 1024, (8192, 8192, 8 * 1024 * 1024)),
        (0, 1, 1, "current_format_dimensions_invalid"),
        (1, 0, 1, "current_format_dimensions_invalid"),
        (8193, 1, 1, "current_format_dimensions_invalid"),
        (1, 8193, 1, "current_format_dimensions_invalid"),
        (1, 1, 0, "frame_size_invalid"),
        (1, 1, 8 * 1024 * 1024 + 1, "frame_size_exceeds_limit"),
    ],
)
def test_current_format_metadata_bounds(
    width: int, height: int, sizeimage: int, expected: tuple[int, int, int] | str
) -> None:
    response = capture_format_request()
    response[8:12] = width.to_bytes(4, "little")
    response[12:16] = height.to_bytes(4, "little")
    response[28:32] = sizeimage.to_bytes(4, "little")
    assert _format_valid(response) == expected


def test_invalid_current_format_response_type_and_size() -> None:
    wrong_type = capture_format_request()
    wrong_type[:4] = (9).to_bytes(4, "little")
    assert _format_valid(wrong_type) == "invalid_current_format_response"
    assert _format_valid(bytearray(207)) == "invalid_current_format_response"


@pytest.mark.parametrize("failure", [MemoryError(), OverflowError()])
def test_frame_buffer_allocation_failure_is_sanitized(failure: Exception) -> None:
    def fail_allocation(_size: int) -> bytearray:
        raise failure

    result = _make_probe(buffer_factory=fail_allocation).probe(identifier="x")
    assert result.reason_code == "frame_buffer_allocation_failed"
    assert result.acquisition_method is None and result.cleanup_completed


@pytest.mark.parametrize("ready_mask", [select.POLLIN, select.POLLRDNORM])
def test_poll_ready_masks_receive_one_frame(ready_mask: int) -> None:
    poller = _Poll(events=[(_FD, ready_mask)])
    result = _make_probe(poll_factory=lambda: poller).probe(identifier="x")
    assert result.reason_code == "validated" and result.frame_received
    assert poller.registrations == [(_FD, select.POLLIN | select.POLLRDNORM)]


def test_fatal_poll_event_has_priority_over_ready_and_unexpected_descriptor() -> None:
    poller = _Poll(events=[(_FD + 1, select.POLLIN | select.POLLERR)])
    result = _make_probe(poll_factory=lambda: poller).probe(identifier="x")
    assert result.reason_code == "poll_fatal_event"


@pytest.mark.parametrize(
    "events, expected",
    [
        ([], "poll_timeout"),
        ([(_FD, select.POLLOUT)], "poll_unexpected_events"),
        ([(_FD + 1, select.POLLIN)], "poll_unexpected_events"),
        ([(_FD, select.POLLIN), (_FD + 1, select.POLLIN)], "poll_unexpected_events"),
    ],
)
def test_poll_nonready_outcomes(events: list[tuple[int, int]], expected: str) -> None:
    result = _make_probe(poll_factory=lambda: _Poll(events=events)).probe(
        identifier="x"
    )
    assert result.reason_code == expected and result.poll_was_attempted
    assert not result.frame_read_was_attempted


def test_poll_eintr_retries_once_with_positive_timeouts() -> None:
    poller = _Poll(failures=[OSError(errno.EINTR, "private")])
    result = _make_probe(poll_factory=lambda: poller).probe(identifier="x")
    assert result.reason_code == "validated" and len(poller.timeouts) == 2
    assert all(timeout > 0 for timeout in poller.timeouts)


def test_poll_eintr_budget_exhaustion() -> None:
    poller = _Poll(
        failures=[OSError(errno.EINTR, "private"), OSError(errno.EINTR, "private")]
    )
    result = _make_probe(poll_factory=lambda: poller).probe(identifier="x")
    assert result.reason_code == "poll_interrupted_budget_exhausted"
    assert len(poller.timeouts) == 2


def test_poll_eintr_retry_stops_when_deadline_expires() -> None:
    poller = _Poll(failures=[OSError(errno.EINTR, "private")])
    result = _make_probe(
        poll_factory=lambda: poller,
        monotonic=_Clock(0.0, 0.0, 0.0, 0.0, 0.0, 2.0),
    ).probe(identifier="x")
    assert result.reason_code == "poll_interrupted_budget_exhausted"
    assert len(poller.timeouts) == 1


@pytest.mark.parametrize("failure", [OSError(errno.EIO, "private"), ValueError()])
def test_poll_construction_failure_maps_to_poll_failed(failure: Exception) -> None:
    def fail_factory() -> _Poll:
        raise failure

    result = _make_probe(poll_factory=fail_factory).probe(identifier="x")
    assert result.reason_code == "poll_failed" and not result.poll_was_attempted


@pytest.mark.parametrize("failure", [OSError(errno.EIO, "private"), ValueError()])
def test_poll_registration_failure_maps_to_poll_failed(failure: Exception) -> None:
    class RegistrationFailure(_Poll):
        def register(self, fd: int, mask: int) -> None:
            raise failure

    result = _make_probe(poll_factory=RegistrationFailure).probe(identifier="x")
    assert result.reason_code == "poll_failed" and not result.poll_was_attempted


def test_generic_poll_oserror_maps_to_poll_failed() -> None:
    poller = _Poll(failures=[OSError(errno.EIO, "private")])
    result = _make_probe(poll_factory=lambda: poller).probe(identifier="x")
    assert result.reason_code == "poll_failed" and result.poll_was_attempted


@pytest.mark.parametrize(
    "error_number, expected",
    [
        (errno.EAGAIN, "frame_not_ready"),
        (errno.EWOULDBLOCK, "frame_not_ready"),
        (errno.ENOSYS, "readv_not_supported"),
        (errno.ENOTSUP, "readv_not_supported"),
        (getattr(errno, "EOPNOTSUPP", errno.ENOTSUP), "readv_not_supported"),
        (errno.EINVAL, "readv_not_supported"),
        (errno.EIO, "frame_read_failed"),
    ],
)
def test_read_error_mappings(error_number: int, expected: str) -> None:
    def fail_read(_fd: int, _buffer: bytearray) -> int:
        raise OSError(error_number, "private")

    result = _make_probe(reader=fail_read).probe(identifier="x")
    assert result.reason_code == expected and result.frame_read_was_attempted
    assert result.frame_buffer_wipe_completed and result.cleanup_completed


def test_readv_eintr_retries_once_then_succeeds() -> None:
    calls = 0

    def reader(fd: int, buffer: bytearray) -> int:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError(errno.EINTR, "private")
        return _reader(fd, buffer)

    result = _make_probe(reader=reader).probe(identifier="x")
    assert result.reason_code == "validated" and calls == 2


def test_readv_eintr_budget_exhaustion() -> None:
    calls = 0

    def reader(_fd: int, _buffer: bytearray) -> int:
        nonlocal calls
        calls += 1
        raise OSError(errno.EINTR, "private")

    result = _make_probe(reader=reader).probe(identifier="x")
    assert result.reason_code == "frame_read_interrupted_budget_exhausted"
    assert calls == 2


def test_readv_eintr_retry_stops_when_deadline_expires() -> None:
    calls = 0

    def reader(_fd: int, _buffer: bytearray) -> int:
        nonlocal calls
        calls += 1
        raise OSError(errno.EINTR, "private")

    result = _make_probe(
        reader=reader,
        monotonic=_Clock(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 2.0),
    ).probe(identifier="x")
    assert result.reason_code == "frame_read_interrupted_budget_exhausted"
    assert calls == 1


@pytest.mark.parametrize(
    "count, expected, received",
    [
        (0, "frame_empty", False),
        (-1, "frame_byte_count_invalid", False),
        (_SIZEIMAGE + 1, "frame_byte_count_invalid", False),
        (1, "validated", True),
        (_SIZEIMAGE, "validated", True),
    ],
)
def test_frame_read_count_bounds(count: int, expected: str, received: bool) -> None:
    result = _make_probe(reader=lambda _fd, _buffer: count).probe(identifier="x")
    assert result.reason_code == expected and result.frame_received is received
    assert result.frame_byte_count == count


def test_read_frame_into_uses_one_iovec_one_mutable_bytearray_and_no_read_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frame = bytearray(b"1234")
    captured: list[memoryview] = []

    def readv(fd: int, iovecs: list[memoryview]) -> int:
        assert fd == _FD and len(iovecs) == 1
        assert iovecs[0].obj is frame and not iovecs[0].readonly
        captured.extend(iovecs)
        return 4

    monkeypatch.setattr(probe_module.os, "readv", readv)
    monkeypatch.setattr(
        probe_module.os,
        "read",
        lambda *_args: (_ for _ in ()).throw(AssertionError("no os.read fallback")),
    )
    assert read_frame_into(_FD, frame) == 4
    with pytest.raises(ValueError):
        captured[0].tobytes()


def test_success_has_one_open_poll_read_and_no_second_acquisition() -> None:
    calls = {"open": 0, "ioctl": 0, "poll": 0, "read": 0, "close": 0}

    class CountingPoll(_Poll):
        def poll(self, timeout: int) -> list[tuple[int, int]]:
            calls["poll"] += 1
            return super().poll(timeout)

    def opener(_target: str, _flags: int) -> int:
        calls["open"] += 1
        return _FD

    def ioctl(fd: int, request: int, buffer: bytearray, mutate: bool) -> int:
        calls["ioctl"] += 1
        return _good_ioctl(fd, request, buffer, mutate)

    def reader(fd: int, buffer: bytearray) -> int:
        calls["read"] += 1
        return _reader(fd, buffer)

    def closer(_fd: int) -> None:
        calls["close"] += 1

    result = _make_probe(
        opener=opener,
        ioctl=ioctl,
        poll_factory=CountingPoll,
        reader=reader,
        closer=closer,
    ).probe(identifier="x")
    assert result.reason_code == "validated"
    assert calls == {"open": 1, "ioctl": 2, "poll": 1, "read": 1, "close": 1}


def test_cleanup_wipes_every_byte_and_verifies_zero() -> None:
    buffer = bytearray(b"sensitive")
    written: list[int] = []

    class TrackingView:
        def __init__(self, value: bytearray) -> None:
            self.view = memoryview(value)

        def __len__(self) -> int:
            return len(self.view)

        def __setitem__(self, index: int, value: int) -> None:
            written.append(index)
            self.view[index] = value

        def __iter__(self):  # type: ignore[no-untyped-def]
            return iter(self.view)

        def release(self) -> None:
            self.view.release()

    result = _make_probe(
        buffer_factory=lambda _size: buffer,
        cleanup_view_factory=TrackingView,
    ).probe(identifier="x")
    assert result.frame_buffer_wipe_completed
    assert written == list(range(len(buffer))) and buffer == bytearray(len(buffer))


def test_wipe_failure_overrides_primary_reason_and_close_failure() -> None:
    class FailingView:
        def __len__(self) -> int:
            return _SIZEIMAGE

        def __setitem__(self, index: int, value: int) -> None:
            raise RuntimeError("private")

        def release(self) -> None:
            pass

    result = _make_probe(
        reader=lambda _fd, _buffer: 0,
        cleanup_view_factory=lambda _buffer: FailingView(),
        closer=lambda _fd: (_ for _ in ()).throw(OSError(errno.EIO, "private")),
    ).probe(identifier="x")
    assert result.reason_code == "frame_buffer_wipe_failed"
    assert not result.frame_buffer_wipe_completed and not result.cleanup_completed


def test_cleanup_view_release_failure_makes_wipe_unconfirmed() -> None:
    class ReleaseFailure:
        def __init__(self, buffer: bytearray) -> None:
            self.view = memoryview(buffer)

        def __len__(self) -> int:
            return len(self.view)

        def __setitem__(self, index: int, value: int) -> None:
            self.view[index] = value

        def __iter__(self):  # type: ignore[no-untyped-def]
            return iter(self.view)

        def release(self) -> None:
            self.view.release()
            raise RuntimeError("private")

    result = _make_probe(cleanup_view_factory=ReleaseFailure).probe(identifier="x")
    assert result.reason_code == "frame_buffer_wipe_failed"
    assert not result.frame_buffer_wipe_completed


def test_close_failure_before_valid_frame_uses_close_reason() -> None:
    result = _make_probe(
        poll_factory=lambda: _Poll(events=[]),
        closer=lambda _fd: (_ for _ in ()).throw(OSError(errno.EIO, "private")),
    ).probe(identifier="x")
    assert result.reason_code == "descriptor_close_failed"
    assert not result.frame_received and not result.cleanup_completed


def test_close_failure_after_valid_frame_is_degraded_reason() -> None:
    result = _make_probe(
        closer=lambda _fd: (_ for _ in ()).throw(OSError(errno.EIO, "private"))
    ).probe(identifier="x")
    assert result.reason_code == "frame_received_cleanup_unconfirmed"
    assert result.frame_received and result.frame_buffer_wipe_completed
    assert not result.descriptor_was_closed and not result.cleanup_completed


def test_wipe_release_reference_drop_close_result_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    real_result = CaptureFrameProbeResult

    class TrackedBuffer(bytearray):
        def __del__(self) -> None:
            events.append("reference_drop")

    class TrackedView:
        def __init__(self, buffer: bytearray) -> None:
            self.view = memoryview(buffer)

        def __len__(self) -> int:
            return len(self.view)

        def __setitem__(self, index: int, value: int) -> None:
            events.append(f"wipe:{index}")
            self.view[index] = value

        def __iter__(self):  # type: ignore[no-untyped-def]
            events.append("verify")
            return iter(self.view)

        def release(self) -> None:
            events.append("release")
            self.view.release()

    def result_factory(*args: object, **kwargs: object) -> CaptureFrameProbeResult:
        events.append("result")
        return real_result(*args, **kwargs)

    monkeypatch.setattr(probe_module, "CaptureFrameProbeResult", result_factory)
    result = _make_probe(
        buffer_factory=TrackedBuffer,
        cleanup_view_factory=TrackedView,
        closer=lambda _fd: events.append("close"),
    ).probe(identifier="x")
    assert result.reason_code == "validated"
    assert events.index("verify") < events.index("release")
    assert events.index("release") < events.index("reference_drop")
    assert (
        events.index("reference_drop") < events.index("close") < events.index("result")
    )
    assert [event for event in events if event.startswith("wipe:")] == [
        f"wipe:{index}" for index in range(_SIZEIMAGE)
    ]


@pytest.mark.parametrize("stage", ["abi", "target", "open", "ioctl", "poll", "read"])
def test_unexpected_injected_exceptions_are_sanitized(stage: str) -> None:
    failure = ValueError("private exception canary")
    overrides: dict[str, Any] = {}
    if stage == "abi":
        overrides["abi_supported"] = lambda: (_ for _ in ()).throw(failure)
    elif stage == "target":
        overrides["target_validator"] = lambda _identifier: (_ for _ in ()).throw(
            failure
        )
    elif stage == "open":
        overrides["opener"] = lambda _target, _flags: (_ for _ in ()).throw(failure)
    elif stage == "ioctl":
        overrides["ioctl"] = lambda *_args: (_ for _ in ()).throw(failure)
    elif stage == "poll":
        overrides["poll_factory"] = lambda: _Poll(failures=[failure])
    else:
        overrides["reader"] = lambda _fd, _buffer: (_ for _ in ()).throw(failure)
    result = _make_probe(**overrides).probe(identifier="x")
    assert result.reason_code == "unexpected_probe_failure"
    assert "private" not in repr(result)


def test_healthy_service_requires_all_invariants() -> None:
    report = validate_capture_frame(
        _SETTINGS, _FakeProbe(_valid_result()), platform="linux"
    )
    assert report.state is ComponentHealthState.HEALTHY
    assert report.reason_code == "validated"
    assert report.frame_byte_count == 4 and report.current_sizeimage == 8


def test_degraded_service_requires_valid_frame_and_unconfirmed_close() -> None:
    result = _valid_result(
        reason_code="frame_received_cleanup_unconfirmed",
        descriptor_was_closed=False,
        cleanup_completed=False,
    )
    report = validate_capture_frame(_SETTINGS, _FakeProbe(result), platform="linux")
    assert report.state is ComponentHealthState.DEGRADED
    assert report.reason_code == "frame_received_cleanup_unconfirmed"


@pytest.mark.parametrize(
    "changes",
    [
        {"cleanup_completed": False},
        {"frame_byte_count": None},
        {"frame_byte_count": 9},
        {"current_width": 0},
        {"acquisition_method": "streaming"},
        {"capability_query_succeeded": False},
        {"poll_was_attempted": False},
        {"streaming_io_was_used": True},
    ],
)
def test_inconsistent_fake_probe_results_become_unexpected(
    changes: dict[str, object],
) -> None:
    report = validate_capture_frame(
        _SETTINGS, _FakeProbe(_valid_result(**changes)), platform="linux"
    )
    assert report.state is ComponentHealthState.UNHEALTHY
    assert report.reason_code == "unexpected_probe_failure"


def test_streaming_io_usage_cannot_be_masked_in_public_report() -> None:
    report = validate_capture_frame(
        _SETTINGS,
        _FakeProbe(_valid_result(streaming_io_was_used=True)),
        platform="linux",
    )
    assert report.reason_code == "unexpected_probe_failure"
    assert report.streaming_io_was_used is True


def test_consistent_unsuccessful_probe_is_unhealthy_with_original_reason() -> None:
    result = CaptureFrameProbeResult(
        "poll_timeout",
        device_was_opened=True,
        descriptor_was_closed=True,
        capability_query_succeeded=True,
        current_format_query_succeeded=True,
        acquisition_method="readwrite",
        poll_was_attempted=True,
        current_width=640,
        current_height=480,
        current_sizeimage=8,
        frame_buffer_wipe_completed=True,
        cleanup_completed=True,
    )
    report = validate_capture_frame(_SETTINGS, _FakeProbe(result), platform="linux")
    assert report.state is ComponentHealthState.UNHEALTHY
    assert report.reason_code == "poll_timeout"


def test_service_sanitizes_probe_exception() -> None:
    report = validate_capture_frame(
        _SETTINGS,
        _FakeProbe(error=RuntimeError("private exception canary")),
        platform="linux",
    )
    assert report.state is ComponentHealthState.UNHEALTHY
    assert report.reason_code == "unexpected_probe_failure"
    assert "private" not in repr(report)


@pytest.mark.parametrize(
    "state, expected_exit",
    [
        (ComponentHealthState.DISABLED, 1),
        (ComponentHealthState.UNHEALTHY, 1),
        (ComponentHealthState.DEGRADED, 1),
        (ComponentHealthState.HEALTHY, 0),
    ],
)
def test_cli_disabled_unhealthy_degraded_and_healthy_output(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    state: ComponentHealthState,
    expected_exit: int,
) -> None:
    report = _report(state, reason_code=state.value)
    monkeypatch.setattr("aurora_core.__main__.validate_capture_frame", lambda _: report)
    monkeypatch.setattr("sys.argv", ["aurora", "hardware", "validate", "capture-frame"])
    assert main() == expected_exit
    output = capsys.readouterr().out
    assert f"Capture-frame validation: {state.value}" in output
    assert f"state: {state.value}" in output and f"reason: {state.value}" in output


def test_cli_prints_optional_sanitized_metadata_and_required_statement(
    capsys: pytest.CaptureFixture[str],
) -> None:
    report = _report(
        ComponentHealthState.HEALTHY,
        acquisition_method="readwrite",
        capability_query_succeeded=True,
        current_format_query_succeeded=True,
        poll_was_attempted=True,
        frame_read_was_attempted=True,
        frame_received=True,
        frame_byte_count=4,
        current_width=640,
        current_height=480,
        current_sizeimage=8,
        frame_buffer_wipe_completed=True,
        descriptor_was_closed=True,
        cleanup_completed=True,
    )
    _print_capture_frame_report(report)
    output = capsys.readouterr().out
    for expected in (
        "acquisition_method: readwrite",
        "capability_query_succeeded: yes",
        "current_format_query_succeeded: yes",
        "poll_attempted: yes",
        "frame_read_attempted: yes",
        "frame_received: yes",
        "frame_byte_count: 4",
        "current_width: 640",
        "current_height: 480",
        "current_sizeimage: 8",
        "frame_buffer_wipe_confirmed: yes",
        "descriptor_closure_confirmed: yes",
        "cleanup_completed: yes",
        "streaming_io_was_used: no",
    ):
        assert expected in output
    assert (
        "One transient userspace frame buffer may have been allocated and wiped. "
        "No V4L2 streaming-buffer negotiation, queueing, mapping, or streaming "
        "ioctl was performed."
    ) in output


def test_cli_omits_absent_optional_metadata_and_preserves_streaming_flag(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _print_capture_frame_report(
        _report(ComponentHealthState.UNHEALTHY, streaming_io_was_used=True)
    )
    output = capsys.readouterr().out
    assert "frame_byte_count:" not in output
    assert "current_width:" not in output and "current_height:" not in output
    assert "current_sizeimage:" not in output
    assert "streaming_io_was_used: yes" in output


def test_cli_output_does_not_expose_privacy_canaries(
    capsys: pytest.CaptureFixture[str],
) -> None:
    report = replace(
        _report(ComponentHealthState.UNHEALTHY),
        message=(
            "/dev/video99 fd=41 errno=5 driver=private-card 0x12345678 "
            "frame-secret credential-secret"
        ),
        reason_code="unexpected_probe_failure",
    )
    _print_capture_frame_report(report)
    output = capsys.readouterr().out
    for canary in (
        "/dev/video99",
        "fd=41",
        "errno=5",
        "private-card",
        "0x12345678",
        "frame-secret",
        "credential-secret",
    ):
        assert canary not in output
