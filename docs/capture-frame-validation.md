# Bounded capture-frame validation

`aurora hardware validate capture-frame --config configs/aurora.local.yaml` is
an explicit operator-only check of one configured V4L2 identifier. It is not a
runtime adapter and does not start the controller.

## Compatibility and permitted operations

The command is gated to Linux on a verified little-endian LP64 ABI: pointers
must be exactly 64 bits, and the private `v4l2_format` layout must match the
208-byte Linux UAPI request used here. An unsupported platform, pointer width,
or byte order fails before target resolution, opening, or any ioctl.

Only `VIDIOC_QUERYCAP` and the 208-byte single-planar `VIDIOC_G_FMT` request are
permitted. Both requests share one four-call probe budget. Every actual ioctl,
including one interrupted by `EINTR`, consumes that shared budget; `G_FMT` can
use only calls left by `QUERYCAP`, and a fifth ioctl is never issued. Exhausting
the budget ends normal probe work so that only cleanup remains.

The probe uses one monotonic deadline for both ioctl requests, polling, and the
frame read. The deadline is checked immediately before every actual ioctl,
poll, and `readv` attempt, including retries after `EINTR`. Polling receives a
bounded positive millisecond timeout while any positive duration remains; an
already expired deadline never causes `poll(0)` or a first frame read. Deadline
expiration stops normal operations but never skips cleanup. If an operation has
already returned `EINTR`, an expired deadline prevents its retry and preserves
that operation's interrupted-budget result rather than reporting ordinary
deadline expiration.

Acquisition is read/write-only: one validated target, one descriptor, one
capability query, one current-format query, one bounded mutable `bytearray`, one
poll registration, and at most one successful `readv` frame. The read uses one
iovec referencing that sole mutable bytearray. There is no `os.read` fallback
and no second acquisition.

V4L2 streaming-buffer negotiation, buffer queueing, mmap, USERPTR, DMABUF,
stream-on, stream-off, format changes, enumeration, discovery, network
activity, DDP, MQTT, HyperHDR, WLED, and runtime-controller activity are
prohibited.

## Buffer handling and cleanup

The buffer size comes only from a validated driver-reported `sizeimage` between
1 byte and 8 MiB. Width and height must each be between 1 and 8192. Frame bytes
are never retained, serialized, printed, logged, or transmitted.

After allocation, cleanup always follows this order, including deadline and
operation failures:

1. overwrite every byte in the transient buffer;
2. verify that every byte is zero;
3. release the cleanup memoryview;
4. drop the mutable bytearray reference;
5. close the descriptor exactly once; and
6. construct the immutable metadata-only result.

No ioctl, poll, or read is allowed after cleanup begins. A wipe failure takes
precedence over the primary result. Without a valid frame, an unconfirmed close
is reported as a descriptor-close failure. With a valid, confirmed-wiped frame,
an unconfirmed close is reported as `frame_received_cleanup_unconfirmed`.
Otherwise the primary sanitized reason is preserved.

## Health rules

`HEALTHY` requires `validated`, read/write acquisition, a received frame with a
present byte count and current width, height, and `sizeimage`, a byte count in
the inclusive range `1..sizeimage`, a confirmed buffer wipe, confirmed
descriptor closure, completed cleanup, and no streaming I/O.

`DEGRADED` is reserved for `frame_received_cleanup_unconfirmed`: the same valid
bounded read/write frame metadata and confirmed wipe must be present, descriptor
closure must be unconfirmed, cleanup must be incomplete, and no streaming I/O
may have occurred. All other enabled results are `UNHEALTHY`. Internally
inconsistent metadata is sanitized to `unexpected_probe_failure`, while the
actual `streaming_io_was_used` flag remains visible in the public report.

All automated coverage uses injected seams and synthetic doubles only.
Compatibility of `readv` with a particular target remains a future explicit
manual hardware check.
