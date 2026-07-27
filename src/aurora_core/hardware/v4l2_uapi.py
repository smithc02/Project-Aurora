"""Private Linux V4L2 UAPI layouts verified from linux/videodev2.h."""

from __future__ import annotations

import struct

_FM = struct.Struct("<III32sIIIII")
_FS = struct.Struct("<III6I2I")
_FI = struct.Struct("<IIIII6I2I")
assert _FM.size == 64 and _FS.size == 44 and _FI.size == 52


def _iow(number: int, size: int) -> int:
    return (1 << 30) | (size << 16) | (ord("V") << 8) | number


def _iowr(number: int, size: int) -> int:
    return (3 << 30) | (size << 16) | (ord("V") << 8) | number


VIDIOC_QUERYCAP = (2 << 30) | (104 << 16) | (ord("V") << 8)
VIDIOC_ENUM_FMT = _iowr(2, _FM.size)
VIDIOC_ENUM_FRAMESIZES = _iowr(74, _FS.size)
VIDIOC_ENUM_FRAMEINTERVALS = _iowr(75, _FI.size)

VIDEO_CAPTURE, VIDEO_CAPTURE_MPLANE = 1, 9
MEMORY_MMAP = 1

CAP_VIDEO_CAPTURE = 1
CAP_VIDEO_CAPTURE_MPLANE = 0x1000
CAP_READWRITE = 0x01000000
CAP_STREAMING = 0x04000000
CAP_DEVICE_CAPS = 0x80000000

FMT_COMPRESSED, FMT_EMULATED, FOURCC_BE = 1, 2, 1 << 31

# LP64-only layouts used by the bounded capture-frame probe. These explicit
# assertions prevent silently using a guessed layout on a different ABI.
_V4L2_FORMAT_SIZE = 208
_V4L2_FORMAT_FMT_OFFSET = 8
_V4L2_PIX_FORMAT_SIZE = 48
_V4L2_REQUESTBUFFERS_SIZE = 20
_V4L2_BUFFER_SIZE = 88

# Single-planar struct v4l2_buffer offsets on little-endian LP64 Linux.
BUFFER_INDEX_OFFSET = 0
BUFFER_TYPE_OFFSET = 4
BUFFER_BYTESUSED_OFFSET = 8
BUFFER_MEMORY_OFFSET = 60
BUFFER_OFFSET_OFFSET = 64
BUFFER_LENGTH_OFFSET = 72

VIDIOC_G_FMT = 0xC0D05604
VIDIOC_REQBUFS = 0xC0145608
VIDIOC_QUERYBUF = 0xC0585609
VIDIOC_QBUF = 0xC058560F
VIDIOC_DQBUF = 0xC0585611
VIDIOC_STREAMON = 0x40045612
VIDIOC_STREAMOFF = 0x40045613

assert VIDEO_CAPTURE == 1
assert MEMORY_MMAP == 1
assert _V4L2_FORMAT_SIZE == 208
assert _V4L2_FORMAT_FMT_OFFSET == 8
assert _V4L2_PIX_FORMAT_SIZE == 48
assert _V4L2_REQUESTBUFFERS_SIZE == 20
assert _V4L2_BUFFER_SIZE == 88

assert VIDIOC_G_FMT == _iowr(4, _V4L2_FORMAT_SIZE)
assert VIDIOC_REQBUFS == _iowr(8, _V4L2_REQUESTBUFFERS_SIZE)
assert VIDIOC_QUERYBUF == _iowr(9, _V4L2_BUFFER_SIZE)
assert VIDIOC_QBUF == _iowr(15, _V4L2_BUFFER_SIZE)
assert VIDIOC_DQBUF == _iowr(17, _V4L2_BUFFER_SIZE)
assert VIDIOC_STREAMON == _iow(18, 4)
assert VIDIOC_STREAMOFF == _iow(19, 4)


def capture_format_request() -> bytearray:
    """Return the fully zeroed LP64 ``VIDIOC_G_FMT`` request."""
    request = bytearray(_V4L2_FORMAT_SIZE)
    request[0:4] = VIDEO_CAPTURE.to_bytes(4, "little")
    return request


def request_buffers_request(*, count: int) -> bytearray:
    """Return one single-planar MMAP ``VIDIOC_REQBUFS`` request."""
    if not 0 <= count <= 0xFFFFFFFF:
        raise ValueError("count is outside the unsigned 32-bit range")
    request = bytearray(_V4L2_REQUESTBUFFERS_SIZE)
    request[0:4] = count.to_bytes(4, "little")
    request[4:8] = VIDEO_CAPTURE.to_bytes(4, "little")
    request[8:12] = MEMORY_MMAP.to_bytes(4, "little")
    return request


def capture_buffer_request(*, index: int = 0) -> bytearray:
    """Return one single-planar MMAP ``struct v4l2_buffer`` request."""
    if not 0 <= index <= 0xFFFFFFFF:
        raise ValueError("index is outside the unsigned 32-bit range")
    request = bytearray(_V4L2_BUFFER_SIZE)
    request[BUFFER_INDEX_OFFSET : BUFFER_INDEX_OFFSET + 4] = index.to_bytes(4, "little")
    request[BUFFER_TYPE_OFFSET : BUFFER_TYPE_OFFSET + 4] = VIDEO_CAPTURE.to_bytes(
        4, "little"
    )
    request[BUFFER_MEMORY_OFFSET : BUFFER_MEMORY_OFFSET + 4] = MEMORY_MMAP.to_bytes(
        4, "little"
    )
    return request


def stream_type_request() -> bytearray:
    """Return the four-byte buffer type used by STREAMON and STREAMOFF."""
    return bytearray(VIDEO_CAPTURE.to_bytes(4, "little"))
