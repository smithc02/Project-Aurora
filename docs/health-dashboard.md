# Read-only health dashboard

Milestone 12 adds a small dependency-free web dashboard that observes Project
Aurora without changing WLED, HyperHDR, capture settings, services, or power.

## Run locally

Set deployment-specific values in the shell rather than committing them:

```bash
export AURORA_WLED_HOST=192.168.1.99
export AURORA_HYPERHDR_HOST=192.168.1.162
export AURORA_HYPERHDR_PORT=8090
export AURORA_CAPTURE_DEVICE=/dev/video0
export AURORA_EXPECTED_LED_COUNT=282
export AURORA_EXPECTED_SKIPPED_LEDS=16
export AURORA_EXPECTED_ACTIVE_LEDS=266
uv run aurora-dashboard
```

Open `http://<raspberry-pi-address>:8080`. The machine-readable endpoint is
`GET /api/health`.

The dashboard binds to `0.0.0.0:8080` by default. It is intended only for a
trusted local network. Do not expose it through router port forwarding.

## What it checks

- WLED read-only `/json/info` and `/json/state` endpoints.
- HyperHDR read-only JSON-RPC `serverinfo`.
- Presence and read access for the configured V4L2 capture device.
- Raspberry Pi temperature, load averages, memory, storage, and uptime.

The capture-card result proves only that the Linux device node is present and
readable. HyperHDR component state provides additional evidence that capture is
active. The dashboard does not claim direct EZCOO telemetry.

## Status policy

- `healthy`: the required observation succeeded and expected values match.
- `degraded`: the service is reachable but a value differs or a warning threshold
  is exceeded.
- `unavailable`: a required endpoint or device cannot be observed.

The overall status is the worst component status. Every request performs fresh,
bounded checks. A failed dashboard check cannot stop HyperHDR or WLED.

## Environment variables

| Variable | Default |
| --- | --- |
| `AURORA_DASHBOARD_BIND_HOST` | `0.0.0.0` |
| `AURORA_DASHBOARD_PORT` | `8080` |
| `AURORA_DASHBOARD_REFRESH_SECONDS` | `5` |
| `AURORA_DASHBOARD_TIMEOUT_SECONDS` | `2.0` |
| `AURORA_WLED_HOST` | empty |
| `AURORA_HYPERHDR_HOST` | `127.0.0.1` |
| `AURORA_HYPERHDR_PORT` | `8090` |
| `AURORA_CAPTURE_DEVICE` | `/dev/video0` |
| `AURORA_EXPECTED_LED_COUNT` | `282` |
| `AURORA_EXPECTED_SKIPPED_LEDS` | `16` |
| `AURORA_EXPECTED_ACTIVE_LEDS` | `266` |
