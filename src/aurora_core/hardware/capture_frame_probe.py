"""Bounded, metadata-only V4L2 read/write single-frame probe.

This module intentionally has no streaming, mmap, discovery, or arbitrary
ioctl API. Its seams make every hardware operation replaceable in tests.
"""

from __future__ import annotations

import errno
import fcntl
import math
import os
import select
import stat
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from aurora_core.hardware import v4l2_uapi as u
from aurora_core.hardware.capture_capability_probe import _open_error_reason
from aurora_core.hardware.models import CaptureFrameProbeResult

_MAX_IOCTLS, _MAX_POLLS, _MAX_READS = 4, 2, 2
_MAX_WIDTH, _MAX_HEIGHT, _MAX_SIZEIMAGE = 8192, 8192, 8 * 1024 * 1024
_DEADLINE_SECONDS = 2.0
_CAPABILITY_SIZE = 104
_FATAL_POLL = select.POLLERR | select.POLLHUP | select.POLLNVAL


class CaptureFrameProbe(Protocol):
    def probe(self, *, identifier: str) -> CaptureFrameProbeResult: ...


def _supported_abi() -> bool:
    return (
        sys.platform == "linux"
        and sys.byteorder == "little"
        and struct_pointer_size() == 8
    )


def struct_pointer_size() -> int:
    import struct

    return struct.calcsize("P")


def read_frame_into(fd: int, buffer: bytearray) -> int:
    """Read one frame into the sole mutable buffer using exactly one iovec."""
    view = memoryview(buffer)
    try:
        return os.readv(fd, [view])
    finally:
        view.release()


class LinuxV4L2CaptureFrameProbe:
    """Perform at most one successful read/write V4L2 frame acquisition."""

    def __init__(
        self,
        *,
        abi_supported: Callable[[], bool] = _supported_abi,
        target_validator: Callable[[str], tuple[str | None, str | None]] | None = None,
        opener: Callable[[str, int], int] = os.open,
        fstat: Callable[[int], os.stat_result] = os.fstat,
        ioctl: Callable[[int, int, bytearray, bool], int] = fcntl.ioctl,
        poll_factory: Callable[[], select.poll] = select.poll,
        reader: Callable[[int, bytearray], int] = read_frame_into,
        buffer_factory: Callable[[int], bytearray] = bytearray,
        cleanup_view_factory: Callable[[bytearray], memoryview] = memoryview,
        closer: Callable[[int], None] = os.close,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._abi_supported = abi_supported
        self._target_validator = target_validator or _validated_target
        self._open, self._fstat, self._ioctl = opener, fstat, ioctl
        self._poll_factory, self._reader = poll_factory, reader
        self._buffer_factory = buffer_factory
        self._cleanup_view_factory = cleanup_view_factory
        self._close, self._monotonic = closer, monotonic

    def probe(self, *, identifier: str) -> CaptureFrameProbeResult:
        """Probe one configured target without retaining sensitive frame bytes."""
        try:
            if not self._abi_supported():
                return CaptureFrameProbeResult("unsupported_abi")
            deadline = self._monotonic() + _DEADLINE_SECONDS
            target, failure = self._target_validator(identifier)
            if failure:
                return CaptureFrameProbeResult(failure)
            if target is None:
                return CaptureFrameProbeResult("unexpected_probe_failure")
            try:
                fd = self._open(
                    target,
                    os.O_RDWR | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0),
                )
            except OSError as error:
                return CaptureFrameProbeResult(_open_error_reason(error))
        except Exception:
            return CaptureFrameProbeResult("unexpected_probe_failure")
        return self._opened(fd, deadline)

    def _opened(self, fd: int, deadline: float) -> CaptureFrameProbeResult:
        reason: str | None = "unexpected_probe_failure"
        cap_ok = fmt_ok = poll_attempted = read_attempted = received = False
        width = height = sizeimage = count = None
        frame_buffer: bytearray | None = None
        buffer_was_allocated = wiped = closed = False
        ioctls_remaining = _MAX_IOCTLS

        try:
            try:
                descriptor_stat = self._fstat(fd)
            except OSError:
                reason = "descriptor_stat_failed"
            except Exception:
                reason = "unexpected_probe_failure"
            else:
                if not stat.S_ISCHR(descriptor_stat.st_mode):
                    reason = "not_character_device"
                else:
                    try:
                        cap = bytearray(_CAPABILITY_SIZE)
                        reason, cap_ok, ioctls_remaining = self._ioctl_with_retry(
                            fd,
                            u.VIDIOC_QUERYCAP,
                            cap,
                            deadline,
                            "capability",
                            ioctls_remaining,
                        )
                        if reason is None:
                            parsed = _capabilities_valid(cap)
                            if parsed == "invalid":
                                reason = "invalid_capability_response"
                            elif not parsed[0]:
                                reason = "single_planar_capture_not_supported"
                            elif not parsed[1]:
                                reason = "readwrite_io_not_supported"
                        if reason is None:
                            if ioctls_remaining == 0:
                                reason = "capability_ioctl_interrupted_budget_exhausted"
                            else:
                                fmt = u.capture_format_request()
                                reason, fmt_ok, ioctls_remaining = (
                                    self._ioctl_with_retry(
                                        fd,
                                        u.VIDIOC_G_FMT,
                                        fmt,
                                        deadline,
                                        "current_format",
                                        ioctls_remaining,
                                    )
                                )
                                if reason is None:
                                    valid = _format_valid(fmt)
                                    if isinstance(valid, str):
                                        reason = valid
                                    else:
                                        width, height, sizeimage = valid
                        if reason is None:
                            assert sizeimage is not None
                            try:
                                frame_buffer = self._buffer_factory(sizeimage)
                                buffer_was_allocated = True
                            except (MemoryError, OverflowError):
                                reason = "frame_buffer_allocation_failed"
                        if reason is None and frame_buffer is not None:
                            if self._monotonic() >= deadline:
                                reason = "validation_deadline_exceeded"
                            else:
                                try:
                                    poller = self._poll_factory()
                                    poller.register(
                                        fd, select.POLLIN | select.POLLRDNORM
                                    )
                                except Exception:
                                    reason = "poll_failed"
                                else:
                                    reason, poll_attempted = self._poll_ready(
                                        poller, fd, deadline
                                    )
                        if reason is None and frame_buffer is not None:
                            reason, read_attempted, received, count = self._read(
                                fd, frame_buffer, deadline
                            )
                    except Exception:
                        reason = "unexpected_probe_failure"
        finally:
            if frame_buffer is not None:
                cleanup_view: memoryview | None = None
                try:
                    cleanup_view = self._cleanup_view_factory(frame_buffer)
                    for index in range(len(cleanup_view)):
                        cleanup_view[index] = 0
                    wiped = not any(cleanup_view)
                except Exception:
                    wiped = False
                finally:
                    if cleanup_view is not None:
                        try:
                            cleanup_view.release()
                        except Exception:
                            wiped = False
                    cleanup_view = None
                frame_buffer = None
            try:
                self._close(fd)
                closed = True
            except Exception:
                closed = False

        if buffer_was_allocated and not wiped:
            reason = "frame_buffer_wipe_failed"
        elif received and wiped and not closed:
            reason = "frame_received_cleanup_unconfirmed"
        elif not closed:
            reason = "descriptor_close_failed"
        cleanup_completed = closed and (not buffer_was_allocated or wiped)
        return CaptureFrameProbeResult(
            reason or "unexpected_probe_failure",
            True,
            closed,
            cap_ok,
            fmt_ok,
            "readwrite" if buffer_was_allocated else None,
            poll_attempted,
            read_attempted,
            received,
            count,
            width,
            height,
            sizeimage,
            wiped,
            cleanup_completed,
            False,
        )

    def _ioctl_with_retry(
        self,
        fd: int,
        request: int,
        buffer: bytearray,
        deadline: float,
        prefix: str,
        attempts_remaining: int,
    ) -> tuple[str | None, bool, int]:
        was_interrupted = False
        while attempts_remaining > 0:
            if self._monotonic() >= deadline:
                reason = (
                    f"{prefix}_ioctl_interrupted_budget_exhausted"
                    if was_interrupted
                    else "validation_deadline_exceeded"
                )
                return reason, False, attempts_remaining
            attempts_remaining -= 1
            try:
                self._ioctl(fd, request, buffer, True)
                return None, True, attempts_remaining
            except OSError as error:
                if error.errno == errno.EINTR:
                    was_interrupted = True
                    if attempts_remaining > 0:
                        continue
                    return (
                        f"{prefix}_ioctl_interrupted_budget_exhausted",
                        False,
                        attempts_remaining,
                    )
                if request == u.VIDIOC_QUERYCAP:
                    reason = (
                        "querycap_not_supported"
                        if error.errno in {errno.ENOTTY, errno.EINVAL}
                        else "capability_query_failed"
                    )
                else:
                    reason = (
                        "current_format_not_supported"
                        if error.errno in {errno.ENOTTY, errno.EINVAL}
                        else "current_format_query_failed"
                    )
                return reason, False, attempts_remaining
            except Exception:
                return "unexpected_probe_failure", False, attempts_remaining
        return f"{prefix}_ioctl_interrupted_budget_exhausted", False, 0

    def _poll_ready(
        self, poller: select.poll, fd: int, deadline: float
    ) -> tuple[str | None, bool]:
        attempts = 0
        while attempts < _MAX_POLLS:
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                reason = (
                    "poll_interrupted_budget_exhausted"
                    if attempts > 0
                    else "validation_deadline_exceeded"
                )
                return reason, attempts > 0
            timeout_ms = max(1, min(math.ceil(remaining * 1000), 2_147_483_647))
            attempts += 1
            try:
                events = poller.poll(timeout_ms)
                if not events:
                    return "poll_timeout", True
                if any(mask & _FATAL_POLL for _event_fd, mask in events):
                    return "poll_fatal_event", True
                if len(events) != 1 or events[0][0] != fd:
                    return "poll_unexpected_events", True
                if events[0][1] & (select.POLLIN | select.POLLRDNORM):
                    return None, True
                return "poll_unexpected_events", True
            except OSError as error:
                if error.errno == errno.EINTR:
                    if attempts < _MAX_POLLS:
                        continue
                    return "poll_interrupted_budget_exhausted", True
                return "poll_failed", True
            except Exception:
                return "unexpected_probe_failure", True
        return "poll_interrupted_budget_exhausted", attempts > 0

    def _read(
        self, fd: int, buffer: bytearray, deadline: float
    ) -> tuple[str, bool, bool, int | None]:
        attempts = 0
        while attempts < _MAX_READS:
            if self._monotonic() >= deadline:
                reason = (
                    "frame_read_interrupted_budget_exhausted"
                    if attempts > 0
                    else "validation_deadline_exceeded"
                )
                return reason, attempts > 0, False, None
            attempts += 1
            try:
                count = self._reader(fd, buffer)
            except OSError as error:
                if error.errno == errno.EINTR:
                    if attempts < _MAX_READS:
                        continue
                    return (
                        "frame_read_interrupted_budget_exhausted",
                        True,
                        False,
                        None,
                    )
                if error.errno in {errno.EAGAIN, errno.EWOULDBLOCK}:
                    return "frame_not_ready", True, False, None
                if error.errno in {
                    errno.ENOSYS,
                    errno.ENOTSUP,
                    getattr(errno, "EOPNOTSUPP", errno.ENOTSUP),
                    errno.EINVAL,
                }:
                    return "readv_not_supported", True, False, None
                return "frame_read_failed", True, False, None
            except Exception:
                return "unexpected_probe_failure", True, False, None
            if count == 0:
                return "frame_empty", True, False, count
            if count < 0 or count > len(buffer):
                return "frame_byte_count_invalid", True, False, count
            return "validated", True, True, count
        return "frame_read_interrupted_budget_exhausted", attempts > 0, False, None


def _capabilities_valid(response: bytearray) -> tuple[bool, bool] | str:
    if len(response) != _CAPABILITY_SIZE or any(response[92:104]):
        return "invalid"
    caps = int.from_bytes(response[84:88], "little")
    device_caps = int.from_bytes(response[88:92], "little")
    effective = device_caps if caps & u.CAP_DEVICE_CAPS else caps
    return bool(effective & u.CAP_VIDEO_CAPTURE), bool(effective & u.CAP_READWRITE)


def _format_valid(response: bytearray) -> tuple[int, int, int] | str:
    if (
        len(response) != 208
        or int.from_bytes(response[0:4], "little") != u.VIDEO_CAPTURE
    ):
        return "invalid_current_format_response"
    width, height, sizeimage = (
        int.from_bytes(response[a:b], "little")
        for a, b in ((8, 12), (12, 16), (28, 32))
    )
    if not (1 <= width <= _MAX_WIDTH and 1 <= height <= _MAX_HEIGHT):
        return "current_format_dimensions_invalid"
    if sizeimage < 1:
        return "frame_size_invalid"
    if sizeimage > _MAX_SIZEIMAGE:
        return "frame_size_exceeds_limit"
    return width, height, sizeimage


def _validated_target(identifier: str) -> tuple[str | None, str | None]:
    # Resolve exactly the configured target; no directory probing or discovery.
    try:
        resolved = str(Path(identifier).resolve(strict=True))
    except FileNotFoundError:
        return None, "device_not_found"
    except (OSError, RuntimeError):
        return None, "symlink_resolution_failed"
    if (
        not resolved.startswith("/dev/video")
        or not resolved.removeprefix("/dev/video").isdigit()
    ):
        return None, "invalid_device_target"
    try:
        mode = os.stat(resolved).st_mode
    except FileNotFoundError:
        return None, "device_not_found"
    except OSError:
        return None, "device_unavailable"
    return (resolved, None) if stat.S_ISCHR(mode) else (None, "not_character_device")
