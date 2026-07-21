# Bounded capture-frame validation

`aurora hardware validate capture-frame --config configs/aurora.local.yaml` is
an explicit operator-only check of one configured V4L2 identifier. It is not a
runtime adapter and does not start the controller.

The command is restricted to verified 64-bit little-endian Linux LP64. It uses
read/write capture only: one validated target, one descriptor, `VIDIOC_QUERYCAP`,
`VIDIOC_G_FMT`, one bounded mutable `bytearray`, one poll registration, and at
most one successful `readv` frame. V4L2 streaming I/O, mmap, USERPTR, DMABUF,
format changes, enumeration, discovery, network activity, DDP, MQTT, HyperHDR,
and WLED are all non-goals and prohibited.

The buffer is allocated only from bounded driver-reported `sizeimage` (at most
8 MiB), overwritten before release, and never retained, serialized, printed, or
logged. Cleanup attempts closure once. A valid wiped frame is healthy when
closure is confirmed, degraded when closure is unconfirmed, and all other
enabled outcomes are unhealthy. CI uses synthetic seams only; compatibility of
`readv` with a particular target remains a future manual hardware check.
