# Read-only WLED validation

Milestones 1–3 established development, configuration, and hardware-free runtime contracts. Milestone 4 adds one explicit operator action:

```bash
uv run aurora hardware validate wled \
  --config configs/aurora.local.yaml
```

It is not run automatically or in CI. The operator supplies an enabled WLED host in an untracked configuration file. The validator sends exactly one unauthenticated `GET /json/info` request, follows no redirects, uses the configured finite timeout (default 2.0 seconds; 0.1–10.0), and accepts at most 64 KiB. It does not print hosts, ports, URLs, IPs, MAC addresses, IDs, credentials, or response bodies.

Only `ver` and `leds.count` are retained. The reported LED count is compared with the sum of all enabled zones only when every enabled zone provides `led_count`; otherwise it is not evaluated. Healthy means valid information and either no complete expected count or a match. Degraded means a valid response with a mismatch. Unhealthy indicates transport or response failure. Disabled means WLED is disabled and no request was made.

Example sanitized result:

```text
WLED validation: healthy
firmware_version: 0.15.0
reported_led_count: 120
expected_led_count: not configured
led_count_match: not evaluated
Read-only validation completed; no WLED state was changed.
HyperHDR, capture, DDP, and the complete lighting path were not tested.
```

Configuration precedence is CLI (`--timeout`) > `AURORA_WLED__VALIDATION_TIMEOUT_SECONDS` > YAML > defaults. No discovery, scanning, polling, MQTT, HyperHDR communication, capture access, DDP, or LED/state control occurs. Success does not prove HDMI, TV, soundbar, wiring, power, capture, HyperHDR, DDP, or the complete ambient-lighting path.
