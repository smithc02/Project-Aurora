"""Pure layout tests for the private bounded V4L2 UAPI definitions."""

from __future__ import annotations

import pytest

from aurora_core.hardware import v4l2_uapi as u


def _u32(buffer: bytearray, offset: int) -> int:
    return int.from_bytes(buffer[offset : offset + 4], "little")


def test_request_buffers_layout_is_exact_and_zeroed() -> None:
    request = u.request_buffers_request(count=1)

    assert len(request) == 20
    assert _u32(request, 0) == 1
    assert _u32(request, 4) == u.VIDEO_CAPTURE
    assert _u32(request, 8) == u.MEMORY_MMAP
    assert request[12:] == b"\0" * 8


def test_zero_count_request_releases_buffers_layout() -> None:
    request = u.request_buffers_request(count=0)

    assert len(request) == 20
    assert _u32(request, 0) == 0
    assert _u32(request, 4) == u.VIDEO_CAPTURE
    assert _u32(request, 8) == u.MEMORY_MMAP


@pytest.mark.parametrize("count", [-1, 0x1_0000_0000])
def test_request_buffers_rejects_out_of_range_count(count: int) -> None:
    with pytest.raises(ValueError):
        u.request_buffers_request(count=count)


def test_capture_buffer_layout_is_exact_and_zeroed() -> None:
    request = u.capture_buffer_request(index=0)

    assert len(request) == 88
    assert _u32(request, u.BUFFER_INDEX_OFFSET) == 0
    assert _u32(request, u.BUFFER_TYPE_OFFSET) == u.VIDEO_CAPTURE
    assert _u32(request, u.BUFFER_MEMORY_OFFSET) == u.MEMORY_MMAP

    expected_nonzero = {
        u.BUFFER_TYPE_OFFSET,
        u.BUFFER_MEMORY_OFFSET,
    }
    for offset in range(0, len(request), 4):
        if offset not in expected_nonzero:
            assert _u32(request, offset) == 0


@pytest.mark.parametrize("index", [-1, 0x1_0000_0000])
def test_capture_buffer_rejects_out_of_range_index(index: int) -> None:
    with pytest.raises(ValueError):
        u.capture_buffer_request(index=index)


def test_stream_type_request_is_exact() -> None:
    request = u.stream_type_request()

    assert request == bytearray(b"\x01\0\0\0")


def test_lp64_ioctl_numbers_are_fixed() -> None:
    assert u.VIDIOC_REQBUFS == 0xC0145608
    assert u.VIDIOC_QUERYBUF == 0xC0585609
    assert u.VIDIOC_QBUF == 0xC058560F
    assert u.VIDIOC_DQBUF == 0xC0585611
    assert u.VIDIOC_STREAMON == 0x40045612
    assert u.VIDIOC_STREAMOFF == 0x40045613


def test_buffer_metadata_offsets_parse_kernel_response() -> None:
    response = u.capture_buffer_request()
    response[u.BUFFER_BYTESUSED_OFFSET : u.BUFFER_BYTESUSED_OFFSET + 4] = (
        4096
    ).to_bytes(4, "little")
    response[u.BUFFER_OFFSET_OFFSET : u.BUFFER_OFFSET_OFFSET + 4] = (8192).to_bytes(
        4, "little"
    )
    response[u.BUFFER_LENGTH_OFFSET : u.BUFFER_LENGTH_OFFSET + 4] = (
        1_048_576
    ).to_bytes(4, "little")

    assert _u32(response, u.BUFFER_BYTESUSED_OFFSET) == 4096
    assert _u32(response, u.BUFFER_OFFSET_OFFSET) == 8192
    assert _u32(response, u.BUFFER_LENGTH_OFFSET) == 1_048_576
