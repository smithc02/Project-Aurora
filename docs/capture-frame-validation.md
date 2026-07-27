# Bounded capture-frame validation

`aurora hardware validate capture-frame --config configs/aurora.local.yaml` is
an explicit operator-only check of one configured V4L2 identifier. It is not a
runtime adapter and does not start the Project Aurora controller.

## Compatibility and permitted operations

The command is gated to Linux on a verified little-endian LP64 ABI. Pointers
must be exactly 64 bits, and the private V4L2 layouts and ioctl numbers used by
the probe must match the Linux UAPI definitions validated by automated tests.

The command resolves only the configured identifier. It does not scan or
discover other video devices.

Acquisition is adaptive:

1. attempt the bounded read/write path;
2. preserve every result other than `readwrite_io_not_supported`; and
3. only when read/write capability is absent, close that probe descriptor and
   perform the bounded single-planar MMAP path.

Neither path changes the selected input or capture format.

## Read/write acquisition path

The read/write path performs:

1. one descriptor open;
2. `VIDIOC_QUERYCAP`;
3. `VIDIOC_G_FMT`;
4. allocation of one bounded mutable userspace buffer;
5. one poll registration;
6. at most one successful `readv` frame;
7. complete buffer wiping and verification; and
8. descriptor closure.

The capability and format requests share a four-call ioctl budget. Every ioctl
attempt, including one interrupted by `EINTR`, consumes that budget.

The frame read uses one iovec referencing the sole mutable bytearray. There is
no `os.read` fallback and no second frame acquisition.

## MMAP acquisition path

The MMAP fallback requires single-planar capture and
`V4L2_CAP_STREAMING`. It performs a bounded sequence:

1. open the configured node;
2. query capabilities and the current format;
3. request one MMAP buffer with `VIDIOC_REQBUFS`;
4. reject a zero returned count or an excessive driver buffer count;
5. query only buffer index zero;
6. map only buffer index zero;
7. queue only buffer index zero;
8. start the capture stream;
9. poll for one frame;
10. dequeue at most one frame;
11. stop the stream;
12. wipe and verify the complete mapped buffer;
13. unmap the buffer;
14. release all driver buffers with `REQBUFS(count=0)`; and
15. close the descriptor.

The driver may report more buffers than requested, but the probe accepts no
more than the configured safety limit and maps or queues only index zero.

The normal MMAP path has one shared fourteen-call ioctl budget. Cleanup ioctls
are separately bounded to at most two attempts each so an interrupted
`STREAMOFF` or buffer-release request cannot create an unbounded retry loop.

## Deadline and polling

Both acquisition paths use one two-second monotonic deadline for normal probe
work. The deadline is checked before normal ioctl, poll, read, and dequeue
attempts.

Polling always receives a positive bounded millisecond timeout while time
remains. An expired deadline never causes `poll(0)` or begins a new frame
operation.

Deadline expiration stops normal acquisition but does not skip cleanup.
Cleanup remains bounded even after the normal deadline expires.

## Frame and buffer bounds

Driver-reported width and height must each be between 1 and 8192.

The current-format `sizeimage` must be between 1 byte and 8 MiB. A dequeued or
read frame byte count must be in the inclusive range `1..sizeimage`.

The mapped buffer length must be positive and no greater than the separate
MMAP safety limit. The dequeued byte count may not exceed either `sizeimage` or
the mapped buffer length.

Frame bytes are never retained, serialized, printed, logged, or transmitted.

## Cleanup

Read/write cleanup overwrites and verifies every byte in the userspace buffer,
releases its memoryview and reference, and closes the descriptor.

MMAP cleanup stops the stream when it was started, wipes the complete mapped
buffer after device writes have stopped, unmaps it, releases driver buffers,
and closes the descriptor.

When `STREAMOFF` cannot be confirmed, the descriptor is closed before the
mapped memory is wiped so the device cannot continue writing while cleanup
modifies the mapping.

Cleanup failures override an otherwise successful acquisition with a sanitized
reason such as:

- `frame_buffer_wipe_failed`
- `stream_stop_failed`
- `buffer_unmap_failed`
- `buffer_release_failed`
- `descriptor_close_failed`
- `frame_received_cleanup_unconfirmed`

No frame content, device path, file descriptor, raw errno text, or driver
response bytes are included in the public report.

## Health rules

`HEALTHY` requires:

- reason `validated`;
- acquisition method `readwrite` or `mmap`;
- one received frame with bounded metadata;
- a confirmed complete buffer wipe;
- confirmed descriptor closure; and
- complete cleanup for the selected acquisition method.

For read/write acquisition, no streaming operation may be reported.

For MMAP acquisition, the report must confirm buffer negotiation, mapping,
queueing, stream start, dequeue attempt, stream stop, unmap, driver-buffer
release, and descriptor closure.

`DEGRADED` is reserved for a valid received and wiped frame where descriptor
closure cannot be confirmed.

All other enabled results are `UNHEALTHY`. Internally inconsistent metadata is
sanitized to `unexpected_probe_failure`, while safety-relevant public fields
remain visible.

Automated coverage uses injected seams and synthetic doubles. Compatibility
with a physical capture device remains an explicit attended operator check and
does not by itself prove live HDMI signal validity, visual correctness,
continuous stability, HyperHDR ingest, or LED output.
