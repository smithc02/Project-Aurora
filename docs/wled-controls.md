# Bounded WLED controls

Milestone 15 adds Project Aurora's first dashboard device mutations. The scope
is deliberately limited to three code-owned operations:

- `wled.power_on`;
- `wled.power_off`; and
- `wled.brightness_set`.

The public portal and `GET /api/health` remain read-only and keep health schema
version 1. No generic execute endpoint, WLED API proxy, arbitrary JSON input, or
browser-supplied operation identifier exists.

## Activation and configuration

WLED control uses a fail-closed two-key activation model plus an explicit
operation allowlist. Dashboard authentication must be enabled and valid, and
`wled.controls.enabled` must separately be true. The configured operation must
also appear in `wled.controls.allowed_operations`, WLED itself must be enabled
with a validated host, and the operation must exist in the code-owned registry.
Authentication alone never activates WLED control.

Existing configurations remain read-only because the control switch defaults to
false and the immutable allowlist defaults to empty. An enabled control switch
with an empty allowlist is valid but exposes no mutations; this makes staged
deployment possible while remaining fail closed.

| Field | Default | Bound |
| --- | --- | --- |
| `wled.controls.enabled` | `false` | Strict boolean. |
| `wled.controls.allowed_operations` | `[]` | Unique list containing only the three identifiers above. |
| `wled.controls.timeout_seconds` | `2.0` | 0.1–5.0 seconds. |
| `wled.controls.maximum_brightness` | `255` | Integer 1–255. |
| `wled.controls.operation_limit` | `20` | Integer 1–120 attempts per client digest and window. |
| `wled.controls.operation_window_seconds` | `60` | Integer 1–3600 seconds. |

Environment equivalents use the existing nested syntax:

```text
AURORA_WLED__CONTROLS__ENABLED
AURORA_WLED__CONTROLS__ALLOWED_OPERATIONS
AURORA_WLED__CONTROLS__TIMEOUT_SECONDS
AURORA_WLED__CONTROLS__MAXIMUM_BRIGHTNESS
AURORA_WLED__CONTROLS__OPERATION_LIMIT
AURORA_WLED__CONTROLS__OPERATION_WINDOW_SECONDS
```

The allowlist environment value is a comma-separated list of exact identifiers.
An empty value means no operations. Control configuration cannot override the
host, port, path, HTTP method, headers, or payload. Keep the WLED host and all
operator values in protected deployment configuration outside the repository.

## Request and input boundary

Every mutation requires an authenticated process-memory session, the valid
per-session CSRF token, form URL encoding, a bounded body, and exactly the
route-specific fields. Unknown or repeated fields, malformed UTF-8 or percent
encoding, transfer encoding, missing or repeated content length, and oversized
bodies are rejected before operation processing.

The fixed server routes select the operation:

| Route | Operation and typed input |
| --- | --- |
| `POST /controls/wled/power-on` | `wled.power_on`; no user payload. |
| `POST /controls/wled/power-off` | `wled.power_off`; fixed explicit confirmation required. |
| `POST /controls/wled/brightness` | `wled.brightness_set`; canonical decimal integer only. |

Brightness must be from 1 through the configured maximum. Zero is not an off
shortcut; booleans, floats, signs, leading-zero forms, relative adjustments,
arrays, and JSON values are not accepted. Power-off requires a separate checked
confirmation value fixed in code. CSRF validation does not count as disruptive
action confirmation.

Successful processing redirects only to `/controls/wled` with a fixed notice
code. Submitted values, endpoint data, credentials, session identifiers, CSRF
tokens, and raw errors never enter a URL.

Milestone 19 Slice Two also presents these same three forms on the canonical
authenticated `/controls` Lighting Controls page. They keep the exact POST routes
above and continue to redirect to the detailed `/controls/wled` page for their
fixed result notice. That presentation change adds no WLED request while the
page renders and does not alter any operation contract.

## Fixed adapter and verification

The synchronous adapter performs exactly one HTTP `POST` to the configured WLED
host at the fixed `/json/state` path. It uses `Content-Type: application/json`,
rejects redirects, applies the configured timeout, reads at most 16 KiB, and
parses one bounded JSON object. It does not retry, queue, follow response URLs,
or send MQTT, DDP, UDP, WebSocket, or shell commands.

Payloads are generated only in code:

```text
{"on": true, "v": true}
{"on": false, "v": true}
{"bri": <validated integer>, "v": true}
```

The brightness value is the validated typed input. Brightness does not include
`on` and therefore does
not implicitly power WLED on or off.

WLED's `v:true` response is supported only as the documented direct state object
needed for verification. Power operations require a top-level Boolean `on`
equal to the requested value. Brightness requires a top-level integer `bri`
equal to the requested value. Nested, missing, wrong-type, malformed, empty,
oversized, or mismatched responses are not verified.

A verified result means only that WLED returned the requested field in this
response. If transport succeeds but state cannot be verified, the portal warns
that the requested change may still have applied. Aurora sends no retry and
attempts no automatic rollback. Network, timeout, HTTP, redirect, size, parse,
missing-state, and mismatch failures become fixed reason codes; endpoint and
response details are not exposed.

## Concurrency, limiting, cache, and audit

One shared Aurora mutation gate now precedes the existing nonblocking WLED lock.
Both are process-local and nonblocking. This prevents a standalone WLED request
from interleaving with the bounded ambient sequence while retaining WLED's
private serialization. A concurrent request receives an immediate sanitized
busy result; there is no wait queue or background worker.

A separate monotonic, in-memory mutation-attempt limiter stores only keyed
digests of bounded client identifiers. It has per-client, global, and memory
caps, cleans expired entries, and resets on process restart. It is independent
of the login limiter and logs no client identifier.

Only a verified mutation invalidates the existing `HealthService` cache.
Invalidation performs no poll and is safe during a single-flight collector
sweep; the next health request performs one fresh bounded sweep. Denied,
malformed, limited, busy, failed, and unverified requests leave the cache intact.
The WLED control page reads current state from that shared snapshot and never
issues its own WLED GET.

Sanitized structured audit events cover success, failure, denial, rate limiting,
busy coordination, confirmation rejection, and verification failure. Audit
fields are limited to schema version, a fixed event, a fixed reason code, and an
allowlisted operation identifier. They exclude brightness, state, endpoints,
URLs, bodies, responses, cookies, sessions, CSRF tokens, raw client identifiers,
and exceptions.

## HyperHDR and realtime interaction

HyperHDR or other WLED realtime input may change observed WLED state immediately
after a verified operation. Milestone 15 does not stop realtime mode, override
live data, or provide “resume ambient” behavior. The next health observation is
informational, not a lock on WLED state.

## Deployment, rollback, and recovery

1. Deploy and validate the application while WLED controls remain disabled.
2. Generate and install the dashboard authentication hash as documented in the
   [control-plane security guide](control-plane-security.md).
3. Put the operator name, hash, validated WLED host, and all local deployment
   values in protected configuration outside the repository.
4. Enable dashboard authentication and confirm login works over the trusted
   local network.
5. Set `wled.enabled` and separately set `wled.controls.enabled`.
6. Add only reviewed identifiers to `wled.controls.allowed_operations`; start
   with the smallest required subset.
7. Set and review timeout, brightness maximum, and attempt-limit bounds.
8. Run `uv run aurora config validate --config <config-file>` and restart the
   externally managed dashboard process.
9. Authenticate, inspect `/api/control/status`, and confirm it lists only the
   intended operations before using `/controls`; `/controls/wled` remains the
   detailed WLED route.

To roll back capability, set `wled.controls.enabled` to false or empty the
allowlist, validate configuration, and restart the externally managed process.
Restarting invalidates sessions and limiter state. It does not undo a WLED state
change that already applied; recovery is operator-owned through separately
reviewed WLED or ambient-lighting procedures. Aurora does not edit configuration
or restart its own service.

## Explicit non-goals

Milestone 15 does not implement brightness zero, toggle or relative adjustment,
presets, effects, speed, intensity, colors, palettes, segments, LED-count or
current-limit changes, network settings, WLED login/PIN support, firmware,
reboot, factory reset, realtime override, DDP or MQTT output, direct HyperHDR
control outside the reviewed ambient composition, automatic ambient resumption,
service or power-supply control, configuration writes,
profiles, backup or rollback automation, room mapping, multi-zone output, frame
analysis, AI, spatial effects, TLS termination, internet exposure, arbitrary
paths, URLs, headers, JSON, HTTP proxying, or shell execution.
