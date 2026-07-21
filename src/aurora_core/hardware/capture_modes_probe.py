"""Fixed, bounded query-only V4L2 mode enumeration."""

from __future__ import annotations

import errno
import fcntl
import os
import stat
from typing import Protocol

from aurora_core.hardware import v4l2_uapi as u
from aurora_core.hardware.capture_capability_probe import (
    _open_error_reason,
    _validated_video_target,
)
from aurora_core.hardware.models import (
    CaptureFrameInterval,
    CaptureFrameSize,
    CaptureModeProbeResult,
    CapturePixelFormat,
)

_MAX_FORMATS, _MAX_SIZES, _MAX_INTERVALS, _MAX_IOCTLS, _MAX_RECORDS = (
    32,
    64,
    64,
    2048,
    512,
)


class _EnumerationLimitReached(Exception):
    pass


class CaptureModeProbe(Protocol):
    def enumerate_modes(self, *, identifier: str) -> CaptureModeProbeResult: ...


class LinuxV4L2ModeProbe:
    def enumerate_modes(self, *, identifier: str) -> CaptureModeProbeResult:
        target, failure = _validated_video_target(identifier)
        if failure:
            return CaptureModeProbeResult(failure)
        assert target
        try:
            fd = os.open(
                target, os.O_RDWR | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
            )
        except OSError as e:
            return CaptureModeProbeResult(_open_error_reason(e))
        result: CaptureModeProbeResult
        query = False
        enum = False
        try:
            try:
                if not stat.S_ISCHR(os.fstat(fd).st_mode):
                    result = CaptureModeProbeResult(
                        "not_character_device", device_was_opened=True
                    )
                else:
                    cap = bytearray(104)
                    try:
                        fcntl.ioctl(fd, u.VIDIOC_QUERYCAP, cap, True)
                        query = True
                    except OSError as e:
                        result = CaptureModeProbeResult(
                            "querycap_not_supported"
                            if e.errno in {errno.ENOTTY, errno.EINVAL}
                            else "capability_query_failed",
                            device_was_opened=True,
                            querycap_was_issued=True,
                        )
                    else:
                        caps = int.from_bytes(cap[84:88], "little")
                        dcaps = int.from_bytes(cap[88:92], "little")
                        effective = dcaps if caps & u.CAP_DEVICE_CAPS else caps
                        queues = []
                        if effective & u.CAP_VIDEO_CAPTURE:
                            queues.append((u.VIDEO_CAPTURE, "single_planar"))
                        if effective & u.CAP_VIDEO_CAPTURE_MPLANE:
                            queues.append((u.VIDEO_CAPTURE_MPLANE, "multi_planar"))
                        if not queues:
                            result = CaptureModeProbeResult(
                                "video_capture_not_supported",
                                device_was_opened=True,
                                querycap_was_issued=True,
                            )
                        else:
                            formats: list[CapturePixelFormat] = []
                            gaps: list[str] = []
                            budget = [1]
                            records = [0]
                            for q, label in queues:
                                try:
                                    # The next operation is an ENUM_FMT attempt.
                                    enum = True
                                    formats.extend(
                                        self._formats(
                                            fd, q, label, budget, records, gaps
                                        )
                                    )
                                except _NestedInvalid as error:
                                    self._gap(gaps, error.code)
                                except _EnumerationLimitReached:
                                    self._gap(gaps, "enumeration_limit_reached")
                                    break
                                except (UnicodeDecodeError, ValueError):
                                    self._gap(gaps, "invalid_format_response")
                                except OSError:
                                    self._gap(gaps, "format_enumeration_failed")
                            if not formats:
                                result = CaptureModeProbeResult(
                                    "no_capture_formats_reported",
                                    (),
                                    False,
                                    tuple(dict.fromkeys(gaps)),
                                    True,
                                    True,
                                    enum,
                                )
                            else:
                                complete = not gaps
                                result = CaptureModeProbeResult(
                                    "validated" if complete else "validated_with_gaps",
                                    tuple(formats),
                                    complete,
                                    tuple(dict.fromkeys(gaps)),
                                    True,
                                    True,
                                    enum,
                                )

            except OSError:
                result = CaptureModeProbeResult(
                    "device_unavailable",
                    device_was_opened=True,
                    querycap_was_issued=query,
                    enumeration_ioctl_was_issued=enum,
                )
        finally:
            try:
                os.close(fd)
                closed = True
            except OSError:
                closed = False
        if not closed:
            return CaptureModeProbeResult(
                "device_unavailable",
                result.formats,
                False,
                result.partial_reason_codes,
                True,
                result.querycap_was_issued,
                result.enumeration_ioctl_was_issued,
                False,
            )
        return CaptureModeProbeResult(
            result.reason_code,
            result.formats,
            result.enumeration_complete,
            result.partial_reason_codes,
            True,
            result.querycap_was_issued,
            result.enumeration_ioctl_was_issued,
            True,
        )

    def _gap(self, gaps: list[str], code: str) -> None:
        if code not in gaps:
            gaps.append(code)

    def _record(self, records: list[int], gaps: list[str]) -> None:
        if records[0] >= _MAX_RECORDS:
            self._gap(gaps, "enumeration_limit_reached")
            raise _EnumerationLimitReached
        records[0] += 1

    def _call(self, fd: int, request: int, buf: bytearray, budget: list[int]) -> None:
        if budget[0] >= _MAX_IOCTLS:
            raise _EnumerationLimitReached
        budget[0] += 1
        fcntl.ioctl(fd, request, buf, True)

    def _formats(
        self,
        fd: int,
        q: int,
        label: str,
        budget: list[int],
        records: list[int],
        gaps: list[str],
    ) -> list[CapturePixelFormat]:
        out: list[CapturePixelFormat] = []
        for index in range(_MAX_FORMATS):
            b = bytearray(64)
            b[:4] = index.to_bytes(4, "little")
            b[4:8] = q.to_bytes(4, "little")
            try:
                self._call(fd, u.VIDIOC_ENUM_FMT, b, budget)
            except _EnumerationLimitReached:
                self._gap(gaps, "enumeration_limit_reached")
                return out
            except OSError as e:
                if e.errno == errno.EINVAL:
                    return out
                raise
            ri, rt, flags, desc, pix, _, *reserved = u._FM.unpack(b)
            if ri != index or rt != q or any(reserved):
                raise ValueError
            nul = desc.find(b"\0")
            if nul < 0:
                raise ValueError
            text = desc[:nul].decode("ascii") if nul else None
            if text and any(ord(c) < 32 or ord(c) == 127 for c in text):
                raise ValueError
            fourcc, be = self._fourcc(pix)
            # Reserve the format record before nested enumeration.
            try:
                self._record(records, gaps)
            except _EnumerationLimitReached:
                return out
            sizes = self._sizes(fd, pix, budget, records, gaps)
            out.append(
                CapturePixelFormat(
                    label,
                    fourcc,
                    be,
                    text,
                    bool(flags & u.FMT_COMPRESSED),
                    bool(flags & u.FMT_EMULATED),
                    tuple(sizes),
                )
            )
        self._gap(gaps, "enumeration_limit_reached")
        return out

    def _fourcc(self, p: int) -> tuple[str, bool]:
        be = bool(p & u.FOURCC_BE)
        p &= ~u.FOURCC_BE
        raw = bytes((p >> (8 * i)) & 255 for i in range(4))
        if any(x < 32 or x > 126 for x in raw):
            raise ValueError
        return raw.decode("ascii"), be

    def _sizes(
        self, fd: int, pix: int, budget: list[int], records: list[int], gaps: list[str]
    ) -> list[CaptureFrameSize]:
        out: list[CaptureFrameSize] = []
        for index in range(_MAX_SIZES):
            b = bytearray(44)
            b[:4] = index.to_bytes(4, "little")
            b[4:8] = pix.to_bytes(4, "little")
            try:
                self._call(fd, u.VIDIOC_ENUM_FRAMESIZES, b, budget)
            except _EnumerationLimitReached:
                self._gap(gaps, "enumeration_limit_reached")
                return out
            except OSError as e:
                if e.errno == errno.EINVAL:
                    if not out:
                        self._gap(gaps, "frame_size_enumeration_unavailable")
                    return out
                self._gap(gaps, "frame_size_enumeration_failed")
                return out
            ri, rp, kind, *v = u._FS.unpack(b)
            if ri != index or rp != pix or any(v[-2:]):
                self._gap(gaps, "invalid_frame_size_response")
                return out
            if kind == 1:
                w, h = v[:2]
                if not w or not h:
                    self._gap(gaps, "invalid_frame_size_response")
                    return out
                try:
                    self._record(records, gaps)
                except _EnumerationLimitReached:
                    return out
                ints, done = self._intervals(fd, pix, w, h, budget, records, gaps)
                out.append(
                    CaptureFrameSize(
                        "discrete",
                        w,
                        h,
                        intervals=tuple(ints),
                        intervals_enumerated=done,
                    )
                )
                continue
            if index or kind not in {2, 3}:
                self._gap(gaps, "invalid_frame_size_response")
                return out
            a, bw, sw, c, d, sh = v[:6]
            if (
                not all((a, bw, c, d))
                or a > bw
                or c > d
                or (kind == 3 and (not sw or not sh))
            ):
                self._gap(gaps, "invalid_frame_size_response")
                return out
            try:
                self._record(records, gaps)
            except _EnumerationLimitReached:
                return out
            self._gap(gaps, "frame_intervals_not_queried_for_range")
            out.append(
                CaptureFrameSize(
                    "continuous" if kind == 2 else "stepwise",
                    min_width=a,
                    max_width=bw,
                    step_width=sw if kind == 3 else None,
                    min_height=c,
                    max_height=d,
                    step_height=sh if kind == 3 else None,
                )
            )
            return out
        self._gap(gaps, "enumeration_limit_reached")
        return out

    def _intervals(
        self,
        fd: int,
        pix: int,
        w: int,
        h: int,
        budget: list[int],
        records: list[int],
        gaps: list[str],
    ) -> tuple[list[CaptureFrameInterval], bool]:
        out: list[CaptureFrameInterval] = []
        for index in range(_MAX_INTERVALS):
            b = bytearray(52)
            b[:4] = index.to_bytes(4, "little")
            b[4:8] = pix.to_bytes(4, "little")
            b[8:12] = w.to_bytes(4, "little")
            b[12:16] = h.to_bytes(4, "little")
            try:
                self._call(fd, u.VIDIOC_ENUM_FRAMEINTERVALS, b, budget)
            except _EnumerationLimitReached:
                self._gap(gaps, "enumeration_limit_reached")
                return out, False
            except OSError as e:
                if e.errno == errno.EINVAL:
                    if not out:
                        self._gap(gaps, "frame_interval_enumeration_unavailable")
                    return out, bool(out)
                self._gap(gaps, "frame_interval_enumeration_failed")
                return out, False
            ri, rp, rw, rh, kind, *v = u._FI.unpack(b)
            if ri != index or rp != pix or rw != w or rh != h or any(v[-2:]):
                self._gap(gaps, "invalid_frame_interval_response")
                return out, False
            if kind == 1:
                n, d = v[:2]
                if not n or not d:
                    self._gap(gaps, "invalid_frame_interval_response")
                    return out, False
                try:
                    self._record(records, gaps)
                except _EnumerationLimitReached:
                    return out, False
                out.append(CaptureFrameInterval("discrete", n, d))
                continue
            if index or kind not in {2, 3}:
                self._gap(gaps, "invalid_frame_interval_response")
                return out, False
            mn, md, mx, xd, sn, sd = v[:6]
            if (
                not all((mn, md, mx, xd))
                or mn * xd > mx * md
                or (kind == 3 and (not sn or not sd))
            ):
                self._gap(gaps, "invalid_frame_interval_response")
                return out, False
            try:
                self._record(records, gaps)
            except _EnumerationLimitReached:
                return out, False
            out.append(
                CaptureFrameInterval(
                    "continuous" if kind == 2 else "stepwise",
                    min_numerator=mn,
                    min_denominator=md,
                    max_numerator=mx,
                    max_denominator=xd,
                    step_numerator=sn if kind == 3 else None,
                    step_denominator=sd if kind == 3 else None,
                )
            )
            return out, True
        self._gap(gaps, "enumeration_limit_reached")
        return out, False


class _NestedInvalid(ValueError):
    def __init__(self, code: str):
        self.code = code
