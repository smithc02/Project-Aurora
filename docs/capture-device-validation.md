# Non-invasive capture-device validation

Milestones 1–5 are complete. Milestone 6 adds the explicit, one-shot `aurora hardware validate capture-device` action for Linux only. Configure exactly one identifier in an untracked local configuration file, then run:

```bash
uv run aurora hardware validate capture-device \
  --config configs/aurora.local.yaml
```

Supported lexical forms are `/dev/videoN`, `/dev/v4l/by-id/<entry>`, and `/dev/v4l/by-path/<entry>`. There is no discovery, directory scanning, globbing, or automatic device selection. CLI `--identifier` overrides environment, which overrides YAML and defaults; no identifier is printed.

On Linux the validator performs bounded metadata checks for the configured node, its final `/dev/videoN` target, character-device type, one derived `/sys/class/video4linux/videoN` entry, and only its `name` attribute. The name read is capped at 256 bytes, rejects unsafe or invalid content, and renders at most 128 characters. An `os.access(..., R_OK)` check is only apparent current process access; it does not guarantee a later open or ioctl succeeds.

`healthy` means those limited presence checks passed; `unhealthy` means one did not; `disabled` means no probe was made. The device is never opened. No ioctl, including `VIDIOC_QUERYCAP`, is issued; formats, resolutions, frame rates, frames, and HDMI signal are not tested. This does not establish capture capability, HyperHDR compatibility, DDP, WLED, LEDs, or full-pipeline operation. CI uses fake probes and never performs manual hardware validation. No permissions, groups, udev rules, or kernel configuration are changed automatically. A later, separately approved milestone may open a node read-only and query capabilities.
