# Query-only V4L2 capability validation

Milestones 1–6 established development, configuration, runtime, WLED,
HyperHDR, and non-opening capture-device boundaries. Milestone 7 adds one
explicit Linux operator action:

```bash
uv run aurora hardware validate capture-capability \
  --config configs/aurora.local.yaml
```

It reuses the configured `capture_device.identifier`; no discovery occurs and
paths, stable identifiers, `bus_info`, and raw capability masks are not printed.
The command alone opens one capture node. Per Linux V4L2 documentation, Aurora
uses `O_RDWR`, plus `O_NONBLOCK` and `O_CLOEXEC` when available. It issues
exactly one `VIDIOC_QUERYCAP` ioctl and closes the descriptor in all outcomes.

The fixed 104-byte `struct v4l2_capability` response is structurally validated.
Driver (ASCII) and card (UTF-8) strings must be NUL-terminated, non-empty, and
control-free; `bus_info` is discarded. Reserved fields must be zero. Aurora
reports the V4L2 API version, single- and multi-planar capture, and streaming
and read/write I/O. When `V4L2_CAP_DEVICE_CAPS` is set, the opened node's
`device_caps` is effective; otherwise `capabilities` is effective.

HEALTHY means the node opened, answered the query, reports ordinary video
capture, and reports streaming or read/write I/O. Output-only and metadata-only
nodes are not accepted. UNHEALTHY reports safe deterministic failures;
DISABLED performs no resolution, open, or ioctl.

This is a query-only, non-streaming, non-mutating ioctl boundary. No format,
resolution, frame-rate, input, control, EDID, or HDMI signal is queried; no
buffer is allocated or mapped; no frame is acquired. HyperHDR compatibility,
DDP, WLED, LEDs, and the full lighting path are not tested. Real hardware
validation is manual and never runs in CI. Bounded format enumeration and frame
acquisition remain separate future approvals.
