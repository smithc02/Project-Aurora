# Read-only health dashboard

Milestone 12 adds a dependency-free local web dashboard and a versioned JSON
health endpoint. It observes Project Aurora through existing validated
configuration and bounded read-only checks. It cannot change WLED or HyperHDR
settings, transmit DDP, restart a service, or control power.

## Configure the installation

Use the same untracked Aurora YAML file as the existing validation commands.
The example below uses non-address hostnames and leaves measured LED values as
placeholders:

```yaml
wled:
  enabled: true
  host: wled.invalid
  validation_timeout_seconds: 2.0
  # expected_led_count: <measured-addressed-count>
  # expected_active_led_count: <measured-active-count>
  # expected_skipped_leds: <measured-skipped-count>
hyperhdr:
  enabled: true
  host: hyperhdr.invalid
  port: 8090
  validation_timeout_seconds: 2.0
capture_device:
  enabled: true
  identifier: /dev/v4l/by-id/REPLACE_WITH_STABLE_DEVICE_NAME
dashboard:
  bind_host: localhost
  port: 8080
  refresh_seconds: 5
  cpu_temperature_warning_c: 80.0
  memory_warning_percent: 90.0
  storage_warning_percent: 90.0
```

When all three optional WLED expected-count fields are configured, the active
and skipped counts must total the addressed count. If `expected_led_count` is
omitted, the existing WLED validator retains its backward-compatible behavior
of deriving an expectation from complete enabled lighting-zone counts.

Nested `AURORA_` environment variables are also supported. For example:

```bash
export AURORA_WLED__ENABLED=true
export AURORA_WLED__HOST=wled.invalid
export AURORA_HYPERHDR__ENABLED=true
export AURORA_HYPERHDR__HOST=hyperhdr.invalid
export AURORA_CAPTURE_DEVICE__ENABLED=true
export AURORA_CAPTURE_DEVICE__IDENTIFIER=/dev/v4l/by-id/REPLACE_WITH_STABLE_DEVICE_NAME
uv run aurora-dashboard --config /path/to/aurora.local.yaml
```

Command-line `--host`, `--port`, and `--refresh-seconds` values override the
dashboard section. Configuration precedence remains CLI, environment, YAML,
then safe defaults.

The default bind host is `localhost`, so the dashboard is local to the Pi. To
make it available on a trusted LAN, explicitly pass `--host 0.0.0.0` and apply
host firewall rules. The service has no authentication or TLS and must not be
exposed through internet-facing port forwarding.

The responsive status page is `/`; the machine-readable endpoint is
`GET /api/health`. Both return successfully even when dependencies are offline.

## Bounded checks

- WLED: the existing hardened `GET /json/info` validator plus one fixed
  `GET /json/state`. Both reject redirects, limit response size, use configured
  finite timeouts, and run concurrently.
- HyperHDR: the existing fixed `GET /json-rpc` `serverinfo` validator. It
  retains only sanitized health fields, including instance, grabber, and LED
  output state when HyperHDR reports them.
- Capture: the existing non-opening V4L2 metadata validator. It validates node
  presence, character-device type, V4L2 registration, read access, and a bounded
  device name. Current activity is inferred only from HyperHDR's grabber flag.
- Raspberry Pi: independent reads of CPU temperature, load averages, memory,
  root storage, and host uptime. A missing metric degrades only this component.

Collectors run concurrently, and unexpected failure in one collector is
converted to a sanitized component failure. A shared single-flight service
caches the snapshot for `dashboard.refresh_seconds`; simultaneous page and API
requests therefore cannot start overlapping hardware polls.

## Status and successful-observation policy

- `healthy`: all required observations for the component succeeded and
  configured expectations match.
- `degraded`: an endpoint responded with malformed or partial data, a configured
  value differs, a component reports inactive, or a host warning threshold is
  exceeded.
- `unavailable`: a required component is disabled, missing, timed out, could not
  be connected to, or failed before any useful observation.

Overall status is the worst component status. `last_successful_at` advances when
a useful observation succeeds and is retained across later timeout or offline
snapshots for the life of the dashboard process.

## JSON schema

`schema_version` is currently `1`. The endpoint returns this shape:

```json
{
  "status": "degraded",
  "checked_at": "2026-01-01T00:00:00+00:00",
  "service_uptime_seconds": 42.0,
  "components": [
    {
      "name": "wled",
      "status": "degraded",
      "message": "WLED was only partially observed or differs from expectations",
      "checked_at": "2026-01-01T00:00:00+00:00",
      "latency_ms": 12.5,
      "details": {
        "info_reason_code": "validated",
        "state_reason_code": "timeout"
      },
      "last_successful_at": "2026-01-01T00:00:00+00:00"
    }
  ],
  "schema_version": 1
}
```

Details vary by component but never include configured hosts, ports, URLs,
credentials, capture paths, response bodies, raw exceptions, IP addresses, or
MAC addresses.

## Optional systemd deployment

[`aurora-dashboard.service.example`](../deploy/systemd/aurora-dashboard.service.example)
is a hardening-oriented example only. Copy and adapt it outside the repository,
create the untracked configuration file, and use normal systemd administration
to install or start it. Aurora does not install, enable, restart, or stop the
unit itself.

## Limitations

- HyperHDR component flags indicate reported enablement, not frame age. The
  dashboard cannot distinguish a fresh frame from a frozen frame and makes no
  claim of direct splitter telemetry.
- The standard library cannot impose a strict wall-clock deadline on DNS
  resolution. Socket reads and connections use bounded configured timeouts, but
  a resolver stall can exceed them.
- Health history is limited to the in-process last-success timestamp. There is
  no metrics database, alerting, automatic recovery, or multi-zone control.
- The dashboard is intentionally read-only and unauthenticated. Put remote
  access behind separately managed authentication and TLS if it is ever needed.
