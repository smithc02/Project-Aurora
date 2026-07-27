"""Bounded Linux V4L2 MMAP single-frame acquisition."""

from __future__ import annotations

import errno
import fcntl
import math
import mmap
import os
import select
import stat
import time
from collections.abc import Callable

from aurora_core.hardware import v4l2_uapi as u
from aurora_core.hardware.capture_capability_probe import _open_error_reason
from aurora_core.hardware.capture_frame_probe import (
    CaptureFrameProbe,
    LinuxV4L2CaptureFrameProbe,
    _format_valid,
    _supported_abi,
    _validated_target,
)
from aurora_core.hardware.models import CaptureFrameProbeResult

_CAPABILITY_SIZE = 104
_DEADLINE_SECONDS = 2.0
_MAX_IOCTLS = 14
_MAX_POLLS = 2
_MAX_DRIVER_BUFFERS = 8
_MAX_MMAP_LENGTH = 16 * 1024 * 1024
_MAX_CLEANUP_IOCTL_ATTEMPTS = 2
_WIPE_CHUNK_SIZE = 64 * 1024
_FATAL_POLL = select.POLLERR | select.POLLHUP | select.POLLNVAL


def map_capture_buffer(
    fd: int,
    length: int,
    flags: int,
    protection: int,
    offset: int,
) -> mmap.mmap:
    """Map exactly one driver-owned V4L2 buffer."""
    return mmap.mmap(
        fd,
        length,
        flags=flags,
        prot=protection,
        offset=offset,
    )


def wipe_mapped_frame(buffer: mmap.mmap) -> bool:
    """Overwrite and verify every byte in one mapped capture buffer."""
    view = memoryview(buffer)
    try:
        zero_chunk = b"\0" * min(_WIPE_CHUNK_SIZE, len(view))
        for start in range(0, len(view), len(zero_chunk)):
            end = min(start + len(zero_chunk), len(view))
            view[start:end] = zero_chunk[: end - start]
        return not any(view)
    finally:
        view.release()


def close_mapped_frame(buffer: mmap.mmap) -> None:
    buffer.close()


def _u32(buffer: bytearray, offset: int) -> int:
    return int.from_bytes(buffer[offset : offset + 4], "little")


def _streaming_capabilities_valid(
    response: bytearray,
) -> tuple[bool, bool] | str:
    if len(response) != _CAPABILITY_SIZE or any(response[92:104]):
        return "invalid"

    capabilities = _u32(response, 84)
    device_capabilities = _u32(response, 88)
    effective = (
        device_capabilities if capabilities & u.CAP_DEVICE_CAPS else capabilities
    )

    return (
        bool(effective & u.CAP_VIDEO_CAPTURE),
        bool(effective & u.CAP_STREAMING),
    )


def _mapped_buffer_valid(response: bytearray) -> tuple[int, int] | str:
    if (
        len(response) != 88
        or _u32(response, u.BUFFER_INDEX_OFFSET) != 0
        or _u32(response, u.BUFFER_TYPE_OFFSET) != u.VIDEO_CAPTURE
        or _u32(response, u.BUFFER_MEMORY_OFFSET) != u.MEMORY_MMAP
    ):
        return "invalid_buffer_query_response"

    length = _u32(response, u.BUFFER_LENGTH_OFFSET)
    offset = _u32(response, u.BUFFER_OFFSET_OFFSET)

    if length < 1:
        return "mapped_buffer_length_invalid"
    if length > _MAX_MMAP_LENGTH:
        return "mapped_buffer_length_exceeds_limit"

    return length, offset


def _dequeued_frame_valid(
    response: bytearray,
    *,
    current_sizeimage: int,
    mapped_length: int,
) -> tuple[int | None, str]:
    if (
        len(response) != 88
        or _u32(response, u.BUFFER_INDEX_OFFSET) != 0
        or _u32(response, u.BUFFER_TYPE_OFFSET) != u.VIDEO_CAPTURE
        or _u32(response, u.BUFFER_MEMORY_OFFSET) != u.MEMORY_MMAP
    ):
        return None, "invalid_frame_dequeue_response"

    byte_count = _u32(response, u.BUFFER_BYTESUSED_OFFSET)

    if byte_count == 0:
        return byte_count, "frame_empty"
    if byte_count > current_sizeimage or byte_count > mapped_length:
        return byte_count, "frame_byte_count_invalid"

    return byte_count, "validated"


class LinuxV4L2AdaptiveCaptureFrameProbe:
    """Prefer read/write acquisition and fall back to bounded MMAP."""

    def __init__(
        self,
        *,
        readwrite_probe: CaptureFrameProbe | None = None,
        streaming_probe: CaptureFrameProbe | None = None,
    ) -> None:
        self._readwrite_probe = readwrite_probe or LinuxV4L2CaptureFrameProbe()
        self._streaming_probe = streaming_probe or LinuxV4L2StreamingCaptureFrameProbe()

    def probe(self, *, identifier: str) -> CaptureFrameProbeResult:
        result = self._readwrite_probe.probe(identifier=identifier)

        if result.reason_code != "readwrite_io_not_supported":
            return result

        return self._streaming_probe.probe(identifier=identifier)


class LinuxV4L2StreamingCaptureFrameProbe:
    """Acquire at most one frame through one mapped V4L2 buffer."""

    def __init__(
        self,
        *,
        abi_supported: Callable[[], bool] = _supported_abi,
        target_validator: Callable[[str], tuple[str | None, str | None]] | None = None,
        opener: Callable[[str, int], int] = os.open,
        fstat: Callable[[int], os.stat_result] = os.fstat,
        ioctl: Callable[[int, int, bytearray, bool], int] = fcntl.ioctl,
        poll_factory: Callable[[], select.poll] = select.poll,
        mapper: Callable[[int, int, int, int, int], mmap.mmap] = map_capture_buffer,
        mapped_wiper: Callable[[mmap.mmap], bool] = wipe_mapped_frame,
        mapped_closer: Callable[[mmap.mmap], None] = close_mapped_frame,
        closer: Callable[[int], None] = os.close,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._abi_supported = abi_supported
        self._target_validator = target_validator or _validated_target
        self._open = opener
        self._fstat = fstat
        self._ioctl = ioctl
        self._poll_factory = poll_factory
        self._mapper = mapper
        self._mapped_wiper = mapped_wiper
        self._mapped_closer = mapped_closer
        self._close = closer
        self._monotonic = monotonic

    def probe(self, *, identifier: str) -> CaptureFrameProbeResult:
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

    def _opened(
        self,
        fd: int,
        deadline: float,
    ) -> CaptureFrameProbeResult:
        reason: str | None = "unexpected_probe_failure"

        capability_query_succeeded = False
        current_format_query_succeeded = False
        acquisition_method: str | None = None
        poll_was_attempted = False
        frame_received = False
        frame_byte_count: int | None = None
        width: int | None = None
        height: int | None = None
        sizeimage: int | None = None

        buffer_negotiation_succeeded = False
        buffer_was_mapped = False
        buffer_was_queued = False
        stream_was_started = False
        frame_dequeue_was_attempted = False
        stream_was_stopped = False
        buffer_was_unmapped = False
        buffers_were_released = False

        frame_buffer_wipe_completed = False
        descriptor_was_closed = False
        mapped_buffer: mmap.mmap | None = None
        mapped_length: int | None = None
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
                    capability = bytearray(_CAPABILITY_SIZE)
                    (
                        reason,
                        capability_query_succeeded,
                        ioctls_remaining,
                    ) = self._ioctl_with_retry(
                        fd,
                        u.VIDIOC_QUERYCAP,
                        capability,
                        deadline,
                        "capability_ioctl_interrupted_budget_exhausted",
                        "querycap_not_supported",
                        "capability_query_failed",
                        ioctls_remaining,
                    )

                    if reason is None:
                        parsed_capabilities = _streaming_capabilities_valid(capability)

                        if parsed_capabilities == "invalid":
                            reason = "invalid_capability_response"
                        elif not parsed_capabilities[0]:
                            reason = "single_planar_capture_not_supported"
                        elif not parsed_capabilities[1]:
                            reason = "streaming_io_not_supported"

                    if reason is None:
                        current_format = u.capture_format_request()
                        (
                            reason,
                            current_format_query_succeeded,
                            ioctls_remaining,
                        ) = self._ioctl_with_retry(
                            fd,
                            u.VIDIOC_G_FMT,
                            current_format,
                            deadline,
                            "current_format_ioctl_interrupted_budget_exhausted",
                            "current_format_not_supported",
                            "current_format_query_failed",
                            ioctls_remaining,
                        )

                        if reason is None:
                            parsed_format = _format_valid(current_format)

                            if isinstance(parsed_format, str):
                                reason = parsed_format
                            else:
                                width, height, sizeimage = parsed_format

                    if reason is None:
                        request_buffers = u.request_buffers_request(count=1)
                        (
                            reason,
                            request_buffers_completed,
                            ioctls_remaining,
                        ) = self._ioctl_with_retry(
                            fd,
                            u.VIDIOC_REQBUFS,
                            request_buffers,
                            deadline,
                            "buffer_negotiation_ioctl_interrupted_budget_exhausted",
                            "buffer_negotiation_not_supported",
                            "buffer_negotiation_failed",
                            ioctls_remaining,
                        )

                        if reason is None and request_buffers_completed:
                            returned_count = _u32(request_buffers, 0)

                            if returned_count < 1:
                                reason = "buffer_negotiation_empty"
                            else:
                                buffer_negotiation_succeeded = True
                                acquisition_method = "mmap"

                                if returned_count > _MAX_DRIVER_BUFFERS:
                                    reason = "buffer_count_exceeds_limit"

                    if reason is None:
                        query_buffer = u.capture_buffer_request(index=0)
                        (
                            reason,
                            query_buffer_completed,
                            ioctls_remaining,
                        ) = self._ioctl_with_retry(
                            fd,
                            u.VIDIOC_QUERYBUF,
                            query_buffer,
                            deadline,
                            "buffer_query_ioctl_interrupted_budget_exhausted",
                            "buffer_query_not_supported",
                            "buffer_query_failed",
                            ioctls_remaining,
                        )

                        if reason is None and query_buffer_completed:
                            parsed_buffer = _mapped_buffer_valid(query_buffer)

                            if isinstance(parsed_buffer, str):
                                reason = parsed_buffer
                            else:
                                mapped_length, mapped_offset = parsed_buffer

                    if reason is None:
                        assert mapped_length is not None
                        try:
                            mapped_buffer = self._mapper(
                                fd,
                                mapped_length,
                                mmap.MAP_SHARED,
                                mmap.PROT_READ | mmap.PROT_WRITE,
                                mapped_offset,
                            )
                            buffer_was_mapped = True
                        except (
                            MemoryError,
                            OSError,
                            OverflowError,
                            ValueError,
                        ):
                            reason = "buffer_map_failed"
                        except Exception:
                            reason = "unexpected_probe_failure"

                    if reason is None:
                        queue_buffer = u.capture_buffer_request(index=0)
                        (
                            reason,
                            buffer_was_queued,
                            ioctls_remaining,
                        ) = self._ioctl_with_retry(
                            fd,
                            u.VIDIOC_QBUF,
                            queue_buffer,
                            deadline,
                            "buffer_queue_ioctl_interrupted_budget_exhausted",
                            "buffer_queue_not_supported",
                            "buffer_queue_failed",
                            ioctls_remaining,
                        )

                    if reason is None:
                        (
                            reason,
                            stream_was_started,
                            ioctls_remaining,
                        ) = self._ioctl_with_retry(
                            fd,
                            u.VIDIOC_STREAMON,
                            u.stream_type_request(),
                            deadline,
                            "stream_start_ioctl_interrupted_budget_exhausted",
                            "stream_start_not_supported",
                            "stream_start_failed",
                            ioctls_remaining,
                        )

                    if reason is None:
                        try:
                            poller = self._poll_factory()
                            poller.register(
                                fd,
                                select.POLLIN | select.POLLRDNORM,
                            )
                        except Exception:
                            reason = "poll_failed"
                        else:
                            reason, poll_was_attempted = self._poll_ready(
                                poller,
                                fd,
                                deadline,
                            )

                    if reason is None:
                        assert sizeimage is not None
                        assert mapped_length is not None

                        dequeue_buffer = u.capture_buffer_request(index=0)
                        (
                            reason,
                            frame_dequeue_was_attempted,
                            ioctls_remaining,
                        ) = self._dequeue_with_retry(
                            fd,
                            dequeue_buffer,
                            deadline,
                            ioctls_remaining,
                        )

                        if reason is None:
                            (
                                frame_byte_count,
                                reason,
                            ) = _dequeued_frame_valid(
                                dequeue_buffer,
                                current_sizeimage=sizeimage,
                                mapped_length=mapped_length,
                            )
                            frame_received = reason == "validated"

        except Exception:
            reason = "unexpected_probe_failure"

        finally:
            if stream_was_started:
                stream_was_stopped = self._cleanup_ioctl(
                    fd,
                    u.VIDIOC_STREAMOFF,
                    u.stream_type_request(),
                )

                # Closing the descriptor before touching the mapping prevents
                # further device writes when STREAMOFF could not be confirmed.
                if not stream_was_stopped:
                    try:
                        self._close(fd)
                        descriptor_was_closed = True
                    except Exception:
                        descriptor_was_closed = False

            if mapped_buffer is not None:
                safe_to_wipe = (
                    not stream_was_started
                    or stream_was_stopped
                    or descriptor_was_closed
                )

                if safe_to_wipe:
                    try:
                        frame_buffer_wipe_completed = self._mapped_wiper(mapped_buffer)
                    except Exception:
                        frame_buffer_wipe_completed = False

                try:
                    self._mapped_closer(mapped_buffer)
                    buffer_was_unmapped = True
                except Exception:
                    buffer_was_unmapped = False

                mapped_buffer = None

            if buffer_negotiation_succeeded and not descriptor_was_closed:
                buffers_were_released = self._cleanup_ioctl(
                    fd,
                    u.VIDIOC_REQBUFS,
                    u.request_buffers_request(count=0),
                )

            if not descriptor_was_closed:
                try:
                    self._close(fd)
                    descriptor_was_closed = True
                except Exception:
                    descriptor_was_closed = False

        cleanup_completed = (
            descriptor_was_closed
            and (not stream_was_started or stream_was_stopped)
            and (
                not buffer_was_mapped
                or (frame_buffer_wipe_completed and buffer_was_unmapped)
            )
            and (not buffer_negotiation_succeeded or buffers_were_released)
        )

        if buffer_was_mapped and not frame_buffer_wipe_completed:
            reason = "frame_buffer_wipe_failed"
        elif stream_was_started and not stream_was_stopped:
            reason = "stream_stop_failed"
        elif buffer_was_mapped and not buffer_was_unmapped:
            reason = "buffer_unmap_failed"
        elif buffer_negotiation_succeeded and not buffers_were_released:
            reason = "buffer_release_failed"
        elif not descriptor_was_closed:
            reason = (
                "frame_received_cleanup_unconfirmed"
                if frame_received
                else "descriptor_close_failed"
            )

        return CaptureFrameProbeResult(
            reason_code=reason or "unexpected_probe_failure",
            device_was_opened=True,
            descriptor_was_closed=descriptor_was_closed,
            capability_query_succeeded=capability_query_succeeded,
            current_format_query_succeeded=(current_format_query_succeeded),
            acquisition_method=acquisition_method,
            poll_was_attempted=poll_was_attempted,
            frame_read_was_attempted=False,
            frame_received=frame_received,
            frame_byte_count=frame_byte_count,
            current_width=width,
            current_height=height,
            current_sizeimage=sizeimage,
            frame_buffer_wipe_completed=(frame_buffer_wipe_completed),
            cleanup_completed=cleanup_completed,
            streaming_io_was_used=(acquisition_method == "mmap"),
            buffer_negotiation_succeeded=(buffer_negotiation_succeeded),
            buffer_was_mapped=buffer_was_mapped,
            buffer_was_queued=buffer_was_queued,
            stream_was_started=stream_was_started,
            frame_dequeue_was_attempted=(frame_dequeue_was_attempted),
            stream_was_stopped=stream_was_stopped,
            buffer_was_unmapped=buffer_was_unmapped,
            buffers_were_released=buffers_were_released,
        )

    def _ioctl_with_retry(
        self,
        fd: int,
        request: int,
        buffer: bytearray,
        deadline: float,
        interrupted_reason: str,
        unsupported_reason: str,
        failed_reason: str,
        attempts_remaining: int,
    ) -> tuple[str | None, bool, int]:
        was_interrupted = False

        while attempts_remaining > 0:
            if self._monotonic() >= deadline:
                return (
                    interrupted_reason
                    if was_interrupted
                    else "validation_deadline_exceeded",
                    False,
                    attempts_remaining,
                )

            attempts_remaining -= 1

            try:
                self._ioctl(fd, request, buffer, True)
                return None, True, attempts_remaining
            except OSError as error:
                if error.errno == errno.EINTR:
                    was_interrupted = True
                    if attempts_remaining > 0:
                        continue
                    return interrupted_reason, False, attempts_remaining

                if error.errno in {errno.ENOTTY, errno.EINVAL}:
                    return (
                        unsupported_reason,
                        False,
                        attempts_remaining,
                    )

                return failed_reason, False, attempts_remaining
            except Exception:
                return (
                    "unexpected_probe_failure",
                    False,
                    attempts_remaining,
                )

        return interrupted_reason, False, 0

    def _dequeue_with_retry(
        self,
        fd: int,
        buffer: bytearray,
        deadline: float,
        attempts_remaining: int,
    ) -> tuple[str | None, bool, int]:
        was_interrupted = False
        attempted = False

        while attempts_remaining > 0:
            if self._monotonic() >= deadline:
                return (
                    "frame_dequeue_ioctl_interrupted_budget_exhausted"
                    if was_interrupted
                    else "validation_deadline_exceeded",
                    attempted,
                    attempts_remaining,
                )

            attempts_remaining -= 1
            attempted = True

            try:
                self._ioctl(
                    fd,
                    u.VIDIOC_DQBUF,
                    buffer,
                    True,
                )
                return None, True, attempts_remaining
            except OSError as error:
                if error.errno == errno.EINTR:
                    was_interrupted = True
                    if attempts_remaining > 0:
                        continue
                    return (
                        "frame_dequeue_ioctl_interrupted_budget_exhausted",
                        True,
                        attempts_remaining,
                    )

                if error.errno in {
                    errno.EAGAIN,
                    errno.EWOULDBLOCK,
                }:
                    return (
                        "frame_not_ready",
                        True,
                        attempts_remaining,
                    )

                if error.errno in {errno.ENOTTY, errno.EINVAL}:
                    return (
                        "frame_dequeue_not_supported",
                        True,
                        attempts_remaining,
                    )

                return (
                    "frame_dequeue_failed",
                    True,
                    attempts_remaining,
                )
            except Exception:
                return (
                    "unexpected_probe_failure",
                    True,
                    attempts_remaining,
                )

        return (
            "frame_dequeue_ioctl_interrupted_budget_exhausted",
            attempted,
            0,
        )

    def _cleanup_ioctl(
        self,
        fd: int,
        request: int,
        buffer: bytearray,
    ) -> bool:
        for _attempt in range(_MAX_CLEANUP_IOCTL_ATTEMPTS):
            try:
                self._ioctl(fd, request, buffer, True)
                return True
            except OSError as error:
                if error.errno == errno.EINTR:
                    continue
                return False
            except Exception:
                return False

        return False

    def _poll_ready(
        self,
        poller: select.poll,
        fd: int,
        deadline: float,
    ) -> tuple[str | None, bool]:
        attempts = 0

        while attempts < _MAX_POLLS:
            remaining = deadline - self._monotonic()

            if remaining <= 0:
                return (
                    "poll_interrupted_budget_exhausted"
                    if attempts > 0
                    else "validation_deadline_exceeded",
                    attempts > 0,
                )

            timeout_ms = max(
                1,
                min(
                    math.ceil(remaining * 1000),
                    2_147_483_647,
                ),
            )
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
                    return (
                        "poll_interrupted_budget_exhausted",
                        True,
                    )

                return "poll_failed", True

            except Exception:
                return "unexpected_probe_failure", True

        return "poll_interrupted_budget_exhausted", attempts > 0
