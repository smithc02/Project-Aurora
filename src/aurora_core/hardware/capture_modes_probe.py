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
        try:
            if not stat.S_ISCHR(os.fstat(fd).st_mode):
                return self._fail("not_character_device", opened=True)
            cap = bytearray(104)
            try:
                fcntl.ioctl(fd, u.VIDIOC_QUERYCAP, cap, True)
            except OSError as e:
                return self._fail(
                    "querycap_not_supported"
                    if e.errno in {errno.ENOTTY, errno.EINVAL}
                    else "capability_query_failed",
                    opened=True,
                    query=True,
                )
            caps = int.from_bytes(cap[84:88], "little")
            dcaps = int.from_bytes(cap[88:92], "little")
            effective = dcaps if caps & u.CAP_DEVICE_CAPS else caps
            queues = (
                [(u.VIDEO_CAPTURE, "single_planar")]
                if effective & u.CAP_VIDEO_CAPTURE
                else []
            )
            if effective & u.CAP_VIDEO_CAPTURE_MPLANE:
                queues.append((u.VIDEO_CAPTURE_MPLANE, "multi_planar"))
            if not queues:
                return self._fail(
                    "video_capture_not_supported", opened=True, query=True
                )
            formats: list[CapturePixelFormat] = []
            gaps: list[str] = []
            budget = [1]
            records = [0]
            for q, label in queues:
                try:
                    formats.extend(self._formats(fd, q, label, budget, records, gaps))
                except ValueError:
                    gaps.append("invalid_format_response")
                except OSError as e:
                    if e.errno != errno.EINVAL:
                        gaps.append("format_enumeration_failed")
            complete = not gaps
            return CaptureModeProbeResult(
                "validated" if complete else "validated_with_gaps",
                tuple(formats),
                complete,
                tuple(dict.fromkeys(gaps)),
                True,
                True,
                budget[0] > 1,
                True,
            )
        finally:
            os.close(fd)

    def _fail(
        self, code: str, *, opened: bool, query: bool = False
    ) -> CaptureModeProbeResult:
        return CaptureModeProbeResult(
            code,
            device_was_opened=opened,
            querycap_was_issued=query,
            descriptor_was_closed=opened,
        )

    def _call(self, fd: int, request: int, buf: bytearray, budget: list[int]) -> None:
        if budget[0] >= _MAX_IOCTLS:
            raise ValueError("enumeration_limit_reached")
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
            b[0:4] = index.to_bytes(4, "little")
            b[4:8] = q.to_bytes(4, "little")
            try:
                self._call(fd, u.VIDIOC_ENUM_FMT, b, budget)
            except OSError as e:
                if e.errno == errno.EINVAL:
                    return out
                raise
            vals = u._FM.unpack(b)
            ri, rt, flags, desc, pix, _, *reserved = vals
            if ri != index or rt != q or any(reserved):
                raise ValueError
            nul = desc.find(b"\0")
            if nul < 0:
                raise ValueError
            text = desc[:nul].decode("ascii") if nul else None
            if text is not None and any(ord(c) < 32 or ord(c) == 127 for c in text):
                raise ValueError
            fourcc, be = self._fourcc(pix)
            if records[0] >= _MAX_RECORDS:
                gaps.append("enumeration_limit_reached")
                return out
            records[0] += 1
            sizes = self._sizes(fd, pix, budget, records, gaps)
            out.append(
                CapturePixelFormat(
                    label,
                    fourcc,
                    be,
                    text,
                    bool(flags & 1),
                    bool(flags & 2),
                    tuple(sizes),
                )
            )
        gaps.append("enumeration_limit_reached")
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
            except OSError as e:
                if e.errno == errno.EINVAL:
                    return out
                gaps.append("frame_size_enumeration_failed")
                return out
            _, _, kind, *v = u._FS.unpack(b)
            if any(v[-2:]):
                raise ValueError
            if kind == 1:
                w, h = v[:2]
                if not w or not h:
                    raise ValueError
                records[0] += 1
                ints = self._intervals(fd, pix, w, h, budget, records, gaps)
                out.append(
                    CaptureFrameSize(
                        "discrete",
                        w,
                        h,
                        intervals=tuple(ints),
                        intervals_enumerated=True,
                    )
                )
                continue
            if index or kind not in {2, 3}:
                raise ValueError
            a, bw, sw, c, d, sh = v[:6]
            if (
                not all((a, bw, c, d))
                or a > bw
                or c > d
                or (kind == 3 and (not sw or not sh))
            ):
                raise ValueError
            records[0] += 1
            gaps.append("frame_intervals_not_queried_for_range")
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
        gaps.append("enumeration_limit_reached")
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
    ) -> list[CaptureFrameInterval]:
        out: list[CaptureFrameInterval] = []
        for index in range(_MAX_INTERVALS):
            b = bytearray(52)
            b[:4] = index.to_bytes(4, "little")
            b[4:8] = pix.to_bytes(4, "little")
            b[8:12] = w.to_bytes(4, "little")
            b[12:16] = h.to_bytes(4, "little")
            try:
                self._call(fd, u.VIDIOC_ENUM_FRAMEINTERVALS, b, budget)
            except OSError as e:
                if e.errno == errno.EINVAL:
                    return out
                gaps.append("frame_interval_enumeration_failed")
                return out
            _, _, _, _, kind, *v = u._FI.unpack(b)
            if any(v[-2:]):
                raise ValueError
            if kind == 1:
                n, d = v[:2]
                if not n or not d:
                    raise ValueError
                records[0] += 1
                out.append(CaptureFrameInterval("discrete", n, d))
                continue
            if index or kind not in {2, 3}:
                raise ValueError
            mn, md, mx, xd, sn, sd = v[:6]
            if (
                not all((mn, md, mx, xd))
                or mn * xd > mx * md
                or (kind == 3 and (not sn or not sd))
            ):
                raise ValueError
            records[0] += 1
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
            return out
        gaps.append("enumeration_limit_reached")
        return out
