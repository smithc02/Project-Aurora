"""Narrow Linux V4L2 ``VIDIOC_QUERYCAP`` probe.

This module deliberately exposes no arbitrary ioctl facility.  Its sole device
operation is one query-only capability ioctl after validating one configured
video node.
"""

from __future__ import annotations

import errno
import fcntl
import os
import re
import stat
import struct
from pathlib import Path
from typing import Protocol

from aurora_core.hardware.models import CaptureCapabilityProbeResult

_VIDEO_NODE = re.compile(r"/dev/video[0-9]+$")
_CAPABILITY_STRUCT = struct.Struct("<16s32s32sIIIIII")
_CAPABILITY_SIZE = 104
assert _CAPABILITY_STRUCT.size == _CAPABILITY_SIZE

# Linux UAPI _IOR('V', 0, struct v4l2_capability).  The UAPI layout uses an
# 8-bit type, 8-bit number, 14-bit size, and 2-bit direction field.
_IOC_READ = 2
_VIDIOC_QUERYCAP = (_IOC_READ << 30) | (_CAPABILITY_SIZE << 16) | (ord("V") << 8)
_V4L2_CAP_VIDEO_CAPTURE = 0x00000001
_V4L2_CAP_VIDEO_CAPTURE_MPLANE = 0x00001000
_V4L2_CAP_READWRITE = 0x01000000
_V4L2_CAP_STREAMING = 0x04000000
_V4L2_CAP_DEVICE_CAPS = 0x80000000


class CaptureCapabilityProbe(Protocol):
    def query(self, *, identifier: str) -> CaptureCapabilityProbeResult: ...


class LinuxV4L2CapabilityProbe:
    """Query exactly one validated Linux V4L2 node and always close it."""

    def query(self, *, identifier: str) -> CaptureCapabilityProbeResult:
        resolved, failure = _validated_video_target(identifier)
        if failure is not None:
            return CaptureCapabilityProbeResult(failure)
        assert resolved is not None
        flags = os.O_RDWR | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
        try:
            descriptor = os.open(resolved, flags)
        except OSError as error:
            return CaptureCapabilityProbeResult(_open_error_reason(error))
        try:
            try:
                if not stat.S_ISCHR(os.fstat(descriptor).st_mode):
                    return CaptureCapabilityProbeResult("not_character_device")
            except OSError:
                return CaptureCapabilityProbeResult("device_unavailable")
            response = bytearray(_CAPABILITY_SIZE)
            try:
                fcntl.ioctl(descriptor, _VIDIOC_QUERYCAP, response, True)
            except OSError as error:
                return CaptureCapabilityProbeResult(_ioctl_error_reason(error))
            try:
                return _parse_response(response)
            except (UnicodeDecodeError, ValueError, struct.error):
                return CaptureCapabilityProbeResult("invalid_capability_response")
        finally:
            os.close(descriptor)


def _validated_video_target(identifier: str) -> tuple[str | None, str | None]:
    """Resolve one permitted identifier without exposing a reusable resolver."""
    try:
        os.lstat(identifier)
    except FileNotFoundError:
        return None, "device_not_found"
    except OSError:
        return None, "symlink_resolution_failed"
    try:
        resolved = str(Path(identifier).resolve(strict=True))
    except (OSError, RuntimeError):
        return None, "symlink_resolution_failed"
    if _VIDEO_NODE.fullmatch(resolved) is None:
        return None, "invalid_device_target"
    try:
        target_stat = os.stat(resolved)
    except FileNotFoundError:
        return None, "device_not_found"
    except OSError:
        return None, "device_unavailable"
    if not stat.S_ISCHR(target_stat.st_mode):
        return None, "not_character_device"
    return resolved, None


def _open_error_reason(error: OSError) -> str:
    if error.errno in {errno.EACCES, errno.EPERM}:
        return "permission_denied"
    if error.errno == errno.EBUSY:
        return "device_busy"
    if error.errno in {errno.ENOENT, errno.ENODEV, errno.ENXIO}:
        return "device_unavailable"
    return "device_open_failed"


def _ioctl_error_reason(error: OSError) -> str:
    if error.errno in {errno.ENOTTY, errno.EINVAL}:
        return "querycap_not_supported"
    return "capability_query_failed"


def _parse_response(response: bytearray) -> CaptureCapabilityProbeResult:
    if len(response) != _CAPABILITY_SIZE:
        raise ValueError("unexpected response size")
    driver, card, _bus_info, version, capabilities, device_caps, *reserved = (
        _CAPABILITY_STRUCT.unpack(response)
    )
    if any(reserved):
        raise ValueError("reserved fields were nonzero")
    effective = device_caps if capabilities & _V4L2_CAP_DEVICE_CAPS else capabilities
    return CaptureCapabilityProbeResult(
        "validated",
        True,
        _decode_fixed(driver, "ascii"),
        _decode_fixed(card, "utf-8"),
        f"{(version >> 16) & 0xFF}.{(version >> 8) & 0xFF}.{version & 0xFF}",
        bool(effective & _V4L2_CAP_VIDEO_CAPTURE),
        bool(effective & _V4L2_CAP_VIDEO_CAPTURE_MPLANE),
        bool(effective & _V4L2_CAP_STREAMING),
        bool(effective & _V4L2_CAP_READWRITE),
    )


def _decode_fixed(value: bytes, encoding: str) -> str:
    terminator = value.find(b"\0")
    if terminator <= 0:
        raise ValueError("missing or empty fixed string")
    text = value[:terminator].decode(encoding)
    if any(ord(character) < 32 or ord(character) == 127 for character in text):
        raise ValueError("control character in fixed string")
    return text
