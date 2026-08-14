# Bounded HyperHDR controls

Milestone 16 adds four authenticated, fail-closed HyperHDR component-state
operations. It does not create a generic JSON-RPC client. The public portal and
`GET /api/health` remain read-only and retain schema version 1.

The implementation is based on HyperHDR's current official
[`componentstate` schema](https://github.com/awawa-dev/HyperHDR/blob/master/sources/api/JSONRPC_schema/schema-componentstate.json)
and
[`HyperAPI` handler](https://github.com/awawa-dev/HyperHDR/blob/master/sources/api/HyperAPI.cpp).
Aurora deliberately exposes only these code-owned mappings:

| Operation | Component | State |
| --- | --- | --- |
| `hyperhdr.video_grabber_enable` | `VIDEOGRABBER` | `true` |
| `hyperhdr.video_grabber_disable` | `VIDEOGRABBER` | `false` |
| `hyperhdr.led_output_enable` | `LEDDEVICE` | `true` |
| `hyperhdr.led_output_disable` | `LEDDEVICE` | `false` |

The browser never submits an operation ID, component, state, endpoint, command,
instance, URL, header, or JSON body. Each route selects one fixed registry entry.

## Activation and configuration

Controls default to disabled with an empty immutable allowlist. An operation is
available only when dashboard authentication is enabled and valid, HyperHDR is
enabled with a validated host and port, `hyperhdr.controls.enabled` is true, and
the operation appears in both the configured allowlist and code registry.
Authentication alone never enables a mutation.

```yaml
hyperhdr:
  enabled: true
  host: hyperhdr.invalid
  port: 8090
  validation_timeout_seconds: 2.0
  controls:
    enabled: true
    allowed_operations:
      - hyperhdr.video_grabber_enable
      - hyperhdr.video_grabber_disable
      - hyperhdr.led_output_enable
      - hyperhdr.led_output_disable
    timeout_seconds: 2.0
    operation_limit: 20
    operation_window_seconds: 60
```

Equivalent environment variables use Aurora's existing nested convention:

```text
AURORA_HYPERHDR__CONTROLS__ENABLED=true
AURORA_HYPERHDR__CONTROLS__ALLOWED_OPERATIONS=hyperhdr.video_grabber_enable,hyperhdr.video_grabber_disable,hyperhdr.led_output_enable,hyperhdr.led_output_disable
AURORA_HYPERHDR__CONTROLS__TIMEOUT_SECONDS=2.0
AURORA_HYPERHDR__CONTROLS__OPERATION_LIMIT=20
AURORA_HYPERHDR__CONTROLS__OPERATION_WINDOW_SECONDS=60
```

The allowlist is a strict comma-separated sequence. Unknown identifiers,
duplicates, empty elements, and whitespace-only elements fail validation. An
empty value means an empty allowlist. Precedence remains CLI overrides,
environment, YAML, then defaults.

The control timeout is strictly 0.1 through 5 seconds. The per-client operation
limit is 1 through 120 and its monotonic window is 1 through 3600 seconds.

## Mutation and verification boundary

An accepted operation performs at most two requests, without retries:

1. exactly one HTTP `POST` to the configured host and port at `/json-rpc`;
2. after a valid acknowledgement, exactly one existing read-only `serverinfo`
   `GET` through the fixed transport and parser.

The POST body is generated in code and has exactly this shape, with the component
and Boolean selected by the registry:

```json
{
  "command": "componentstate",
  "componentstate": {
    "component": "VIDEOGRABBER",
    "state": true
  }
}
```

The mutation adapter sends fixed JSON headers, rejects redirects, applies one
bounded timeout, and reads at most 16 KiB. It requires a top-level JSON object
whose `success` member is exactly Boolean `true`; when `command` is present it
must equal `componentstate`. Missing, malformed, wrong-type, failed,
unauthorized, oversized, redirected, or mismatched responses fail safely.

Acknowledgement is not verified success. `VIDEOGRABBER` operations require the
parsed `grabber_active` Boolean to match; `LEDDEVICE` operations require
`led_output_active` to match. A timeout, malformed response, missing or ambiguous
component, or opposite Boolean after acknowledgement returns a fixed unverified
warning: the change may have applied. Aurora does not retry, follow a
response-provided URL, roll back, or restore an earlier state.

A matching serverinfo flag proves only HyperHDR's reported component state. It
does not prove physical LED output, capture-frame freshness, HDMI signal, WLED
state, wiring, or power state.

## Request, concurrency, and cache policy

The protected routes are:

- `GET /controls` for the unified Lighting Controls presentation
- `GET /controls/hyperhdr`
- `POST /controls/hyperhdr/video-grabber/enable`
- `POST /controls/hyperhdr/video-grabber/disable`
- `POST /controls/hyperhdr/led-output/enable`
- `POST /controls/hyperhdr/led-output/disable`

Every POST requires an authenticated process-memory session, its valid CSRF
token, strict `application/x-www-form-urlencoded` parsing, one content length,
no transfer encoding, bounded body size, valid encoding, and exact
route-specific fields. Disabling the video grabber requires confirmation that
capture will be interrupted. Disabling LED output separately requires
confirmation that LED transmission will be interrupted. Enable operations need
no disruptive confirmation.

One nonblocking process-local lock covers all HyperHDR mutations. An overlap is
rejected as busy rather than queued. A separate HyperHDR mutation limiter uses
the bounded per-client, global, expiry-cleanup, and maximum-client protections;
it does not share or change WLED limiter state.

The page displays HyperHDR fields from the shared cached health snapshot and
does not poll independently. Only a verified success invalidates the existing
cache, without polling. Denied, malformed, rate-limited, busy, failed, and
unverified outcomes leave it unchanged. The next normal health request refreshes
the snapshot.

Milestone 19 Slice Two reuses the same four forms on `/controls`; each keeps its
original component-specific POST handler and result redirect, and the detailed
route remains functional. Merely rendering either page never invokes the
HyperHDR mutation adapter.

## Audit and disclosure boundary

Mutation audit records contain only schema version 1, a fixed event identifier,
a fixed reason code, and an allowlisted operation ID. They never contain state,
host, port, URL, endpoint, request or response bodies, raw exceptions, cookies,
sessions, CSRF tokens, client identifiers, credentials, configuration, or
instance identifiers. Redirect notices are fixed and never include submitted
values or raw errors.

## Deployment and rollback

1. Back up the untracked deployment configuration.
2. Configure and validate dashboard authentication.
3. Configure the existing HyperHDR host and explicit port.
4. Keep `hyperhdr.controls.enabled: false` while validating configuration.
5. Add only required operations to `allowed_operations`, then enable the
   separate switch.
6. Run `uv run aurora config validate --config <config-file>` and restart the
   separately managed dashboard service.
7. Authenticate locally, inspect `/controls`, and test one operation at a time
   on the trusted LAN under operator observation.

To roll back, set `hyperhdr.controls.enabled: false` or clear its allowlist,
validate, and restart the dashboard process. Reverting the application release
is optional; no database, persistent session, or HyperHDR configuration
migration exists. Aurora never installs or controls the HyperHDR service.

## Limitations and non-goals

HyperHDR's HTTP API credential/token support is not available through this
adapter, so the endpoint must remain on a trusted, firewall-controlled LAN.
Aurora supplies neither TLS nor an internet-facing security boundary.

Milestone 16 has no `ALL` component control, browser-selected component or state,
generic executor, arbitrary JSON-RPC or HTTP proxy, instance operation, service
or process restart, HDR-mode change, effect, color, priority, smoothing,
configuration, calibration, preset, WebSocket, MQTT, DDP, UDP, shell, subprocess,
profile, automation, or combined ambient operation. WLED behavior is unchanged.
Combined operations remain deferred to later profile and automation milestones.
