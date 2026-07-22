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
CAP_READWRITE = 0x01000000
FMT_COMPRESSED, FMT_EMULATED, FOURCC_BE = 1, 2, 1 << 31

# LP64-only layout used by the bounded read/write frame probe.  These explicit
# assertions prevent silently using a guessed layout on a different ABI.
_V4L2_FORMAT_SIZE = 208
_V4L2_FORMAT_FMT_OFFSET = 8
_V4L2_PIX_FORMAT_SIZE = 48
VIDIOC_G_FMT = 0xC0D05604
assert VIDEO_CAPTURE == 1
assert _V4L2_FORMAT_SIZE == 208
assert _V4L2_FORMAT_FMT_OFFSET == 8
assert _V4L2_PIX_FORMAT_SIZE == 48
assert VIDIOC_G_FMT == _iowr(4, _V4L2_FORMAT_SIZE)


def capture_format_request() -> bytearray:
    """Return the fully zeroed LP64 ``VIDIOC_G_FMT`` request."""
    request = bytearray(_V4L2_FORMAT_SIZE)
    request[0:4] = VIDEO_CAPTURE.to_bytes(4, "little")
    return request
