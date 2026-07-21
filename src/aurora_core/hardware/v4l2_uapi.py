"""Private Linux V4L2 UAPI layouts verified from linux/videodev2.h."""

from __future__ import annotations

import struct

_FM = struct.Struct("<III32sIIIII")
_FS = struct.Struct("<III6I2I")
_FI = struct.Struct("<IIIII6I2I")
assert _FM.size == 64 and _FS.size == 44 and _FI.size == 52


def _iowr(number: int, size: int) -> int:
    return (3 << 30) | (size << 16) | (ord("V") << 8) | number


VIDIOC_QUERYCAP = (2 << 30) | (104 << 16) | (ord("V") << 8)
VIDIOC_ENUM_FMT = _iowr(2, _FM.size)
VIDIOC_ENUM_FRAMESIZES = _iowr(74, _FS.size)
VIDIOC_ENUM_FRAMEINTERVALS = _iowr(75, _FI.size)
VIDEO_CAPTURE, VIDEO_CAPTURE_MPLANE = 1, 9
CAP_VIDEO_CAPTURE, CAP_VIDEO_CAPTURE_MPLANE, CAP_DEVICE_CAPS = 1, 0x1000, 0x80000000
FMT_COMPRESSED, FMT_EMULATED, FOURCC_BE = 1, 2, 1 << 31
