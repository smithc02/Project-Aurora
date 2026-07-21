# Read-only HyperHDR validation

Milestones 1–4 are complete. Milestone 5 adds this explicit, one-shot operator
action:

```bash
uv run aurora hardware validate hyperhdr \
  --config configs/aurora.local.yaml
```

The operator supplies an enabled host in an untracked configuration file. The
validator sends exactly one HTTP GET to fixed `/json-rpc`; its only query key is
an internally URL-encoded compact JSON command, `{"command":"serverinfo"}`.
It never uses POST, HTTPS, authentication, discovery, scanning, MQTT, or WLED.
It rejects redirects, uses `hyperhdr.validation_timeout_seconds` (default 2.0,
range 0.1–10.0), defaults an unspecified port to 8090, and accepts at most 256
KiB. Endpoint details, response bodies, credentials, and raw errors are not
printed.

`healthy` means the read-only serverinfo API returned JSON with `success: true`
and an object `info`; `unhealthy` means transport or schema failure; `disabled`
means no request was made. Optional `info.videomodehdr` boolean or integer 0/1
is summarized as `hdr_mode`; it is a reported server setting, not proof that an
HDR signal is captured. Authentication-required responses are unhealthy because
authentication is not implemented.

Successful validation proves only that the read-only serverinfo API responded.
It does not prove capture-card operation, HDMI input or signal, HDR detection,
tone mapping, WLED output, DDP, LED operation, or the complete pipeline. No
HyperHDR state, component, instance, capture process, or HDR mode is changed;
no images or LED data are sent. Manual Raspberry Pi validation is deliberately
not performed in CI. Precedence is CLI `--timeout`, environment
`AURORA_HYPERHDR__VALIDATION_TIMEOUT_SECONDS`, YAML, then defaults.
