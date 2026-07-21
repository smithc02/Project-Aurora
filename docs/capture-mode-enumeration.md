# Bounded V4L2 capture-mode enumeration

Milestones 1–7 are complete. Milestone 8 adds the Linux-only, explicit
`aurora hardware validate capture-modes` operator command. It reuses the one
configured `capture_device.identifier`; it performs no discovery and prints no
path or hardware identifier.

The command validates one final `/dev/videoN` character node, opens it exactly
once with `O_RDWR`, `O_NONBLOCK`, and `O_CLOEXEC` when available, issues exactly
one `VIDIOC_QUERYCAP`, then uses only `VIDIOC_ENUM_FMT`,
`VIDIOC_ENUM_FRAMESIZES`, and `VIDIOC_ENUM_FRAMEINTERVALS`. It closes the
descriptor deterministically. This is query-only, bounded, and non-streaming;
it is not described as read-only because V4L2 requires `O_RDWR`.

Single-planar and multi-planar capture queues are enumerated independently.
Format driver order is preserved (and may indicate preference); frame-size and
interval order has no preference meaning. FourCC is rendered only after safe
printable-ASCII normalization, with the big-endian bit represented separately.
Only compressed and emulated flags are retained.

Discrete sizes and intervals are enumerated until `EINVAL`. Stepwise and
continuous sizes/intervals are represented as ranges at index zero and are not
expanded. Intervals are only queried for discrete sizes. Frame intervals are
exact rational seconds; displayed FPS is the reciprocal. Enumerated modes are
driver claims only and do not prove frame acquisition.

Private fixed safety limits are 32 formats per queue, 64 discrete sizes per
format, 64 intervals per size, 2048 ioctl attempts, and 512 normalized records.
A limit or nested gap produces `degraded`, not healthy. `healthy` requires a
complete valid enumeration with formats; `unhealthy` has no useful format data;
`disabled` performs no resolution, open, or ioctl.

No format/current-format operation, input selection, control change, buffer
allocation/mapping, streaming, frame acquisition, HDMI signal query, HyperHDR,
DDP, or WLED operation occurs. Hardware execution is manual and excluded from
CI. Frame acquisition requires a separately approved future milestone.

```bash
uv run aurora hardware validate capture-modes --config configs/aurora.local.yaml
```
