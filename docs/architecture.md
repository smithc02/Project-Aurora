# Architecture

```mermaid
flowchart LR
  PS5[PS5] --> S[EZCOO EZ-SP12H21 HDMI 2.1 splitter]
  S -->|Output 1: direct high-bandwidth HDMI| TV[LG OLED C9 65-inch TV]
  TV -->|eARC| Soundbar[Vizio Atmos soundbar]
  S -->|Output 2: scaled 1080p60| Capture[Hagibis MS2130 UVC capture card]
  Capture --> Pi[Raspberry Pi 5 / HyperHDR]
  Pi -->|DDP over UDP via Ethernet| WLED[QuinLED Dig-Quad / WLED]
  WLED --> LEDs[BTF-LIGHTING WS2815 12 V LED strip]
  MQTT[(Future MQTT)] -. automation, configuration, telemetry only .-> Pi
  MQTT -. never real-time frames .-> WLED
```

## Data-flow boundaries

The splitter's first output is the high-bandwidth PS5-to-TV path. Aurora does
not sit inline after that output and must not compromise 4K120, VRR, HDR, eARC,
or Atmos. Only the secondary, scaled 1080p60 output is captured.

HyperHDR on the Raspberry Pi 5 is the initial capture and screen-color
extraction component. The Pi and Dig-Quad communicate over Ethernet. WLED owns
physical LED control; Project Aurora does not replace either component in this
milestone.

## Transport roles

DDP over UDP is the planned real-time frame transport because it is intended
for frequent LED-frame delivery with low overhead. MQTT is deliberately
reserved for future automation, configuration, and telemetry. MQTT must not
transport real-time LED frames.

## Future zones

The first planned zone is the rear perimeter of the 65-inch LG C9. Future
independent zones should be represented as separately named, configurable
entities with their own mapping and endpoint configuration. LED counts,
addresses, ports, and layout orientation must be measured and configured, never
embedded in code or example defaults.

## Configuration boundary

Milestone 2 supplies a validated configuration model for the planned components
only. It loads safe defaults, an explicitly selected YAML file, `AURORA_`
environment overrides, and CLI overrides without connecting to a device. The
configuration model does not establish any hardware or network integration.

## Runtime boundary (Milestone 3)

`aurora_core.config` owns loading and validates one `AuroraSettings` snapshot.
Runtime planning accepts that snapshot only and creates an immutable,
secret-free `RuntimePlan`; it neither reads files or environment variables nor
contacts devices. The plan always orders components as capture device, HyperHDR,
WLED, DDP, then MQTT. Zones and LED layout are summarized as resources, not
startable components.

Future adapters must be injected through the synchronous component contract
(`component_id`, `start`, `stop`, and `health`). The controller starts enabled
components in plan order and stops successful starts in reverse order, including
startup rollback. Lifecycle states are created, starting, running, stopping,
stopped, and failed. Health states are disabled, unknown, healthy, degraded,
and unhealthy; valid configuration begins as unknown.

Overall health is disabled with no enabled components, then prioritizes
unhealthy/failed, degraded, unknown (including missing reports), and healthy.
No automatic reload, file watching, environment rereading, polling, or adapter
implementation exists. To apply configuration changes, stop the controller and
create a new settings snapshot, plan, and controller.


## Read-only WLED boundary (Milestone 4)

The explicit `aurora hardware validate wled` operator command makes one GET
request to WLED's fixed `/json/info` endpoint, with a finite configured timeout
and a 64 KiB response limit. Its public sanitized report retains firmware
version, LED count, uptime, and current-limit observations when reported. It
does not start the runtime controller, transmit DDP, alter WLED state, or
validate HyperHDR or capture hardware. A future runtime adapter requires
separate approval.

## Read-only HyperHDR boundary (Milestone 5)

`aurora hardware validate hyperhdr` is a separate explicit operator command. It
makes exactly one HTTP GET to fixed `/json-rpc`, URL-encoding only the fixed
`{"command":"serverinfo"}` request. It has a finite timeout (default 2.0
seconds), rejects redirects, and limits responses to 256 KiB. Its sanitized
report retains successful server-information status, optional `videomodehdr`,
and selected instance, grabber, and LED-output booleans when reported; it
neither changes HyperHDR state nor contacts WLED, starts capture, sends DDP, or
starts the runtime controller.

## Capture-device boundary (Milestone 6)

The explicit capture validation command performs bounded local Linux metadata
inspection of one configured identifier. It never opens the node or issues an
ioctl, and it neither contacts HyperHDR/WLED nor starts the runtime controller.
See [capture-device validation](capture-device-validation.md).

## Query-only V4L2 capability boundary (Milestone 7)

The explicit `aurora hardware validate capture-capability` command resolves one
configured identifier, opens one V4L2 node, issues only `VIDIOC_QUERYCAP`, and
closes the descriptor. It uses node-specific `device_caps` when Linux signals
`V4L2_CAP_DEVICE_CAPS`; otherwise it uses `capabilities`. It does no frame,
format, buffer, streaming, network, or runtime operation. See
[capture capability validation](capture-capability-validation.md).

## Bounded V4L2 mode boundary (Milestone 8)

The explicit `capture-modes` command opens one configured node once, queries
capabilities, and performs only bounded V4L2 format, size, and interval
enumeration. It does not configure capture or acquire frames; runtime remains
hardware-free. See [capture mode enumeration](capture-mode-enumeration.md).

Milestone 9 adds a separate operator-only `capture-frame` boundary. It permits
capability and current-format queries plus one bounded frame acquisition on one
configured target. Read/write capture is preferred. When the device does not
support read/write I/O, the command may fall back to a bounded single-planar
MMAP path that requests driver buffers, maps and queues exactly one buffer,
starts the stream, dequeues at most one frame, and then stops, wipes, unmaps,
releases, and closes all resources. It never performs continuous capture,
transmits frame data, or retains frame content.

## Bounded DDP output boundary (Milestone 10)

`aurora hardware validate ddp-output` is a separate, explicit operator action,
not a runtime adapter. Configuration must enable DDP and provide its host, and
exactly one enabled `lighting_zones` entry must provide 1–512 LEDs. The service
constructs and validates the complete test and blackout packet plans before it
resolves the configured destination once or creates one UDP socket.

The command submits at most one static RGB(0, 0, 16) frame and then enters the
blackout phase exactly once. Blackout is attempted after socket creation even if
test submission fails. One two-second monotonic transmission/send deadline is
established before resolution, so resolution and socket creation consume the
remaining send budget and no `sendto` call is permitted after it expires. The
standard-library `getaddrinfo` call is blocking and cannot be forcibly
interrupted by this implementation; neither resolution nor total command
wall-clock duration is claimed to be capped at two seconds. If resolution
returns after the deadline, the socket is still created, no test packet is sent,
and the blackout phase is entered under the same expired deadline. There is no
retry or address fallback, at most two datagrams per frame, and at most four send
calls. Resolution accepts exactly one unique IPv4 or IPv6 unicast UDP
destination; discovery, broadcast, multicast, scanning, and mDNS browsing are
absent.

The socket is closed after blackout and no later network operation is permitted.
A blackout failure overrides test and cleanup outcomes. Successful submission of
both frames with unconfirmed socket closure is degraded; an earlier test failure
remains unhealthy even when blackout succeeds. UDP submission has no receipt
acknowledgment and makes no claim about WLED receipt, LEDs, or the complete
lighting path. Runtime and continuous DDP transmission remain unimplemented. See
[bounded DDP output validation](ddp-output-validation.md).

## Single-zone baseline proof boundary (Milestone 11)

Milestone 11 defines an operator-controlled proof and deployment runbook for the
existing single-zone architecture. It does not replace or add components to the
direct PS5-to-TV path, the secondary capture path, HyperHDR, WLED, or the one
configured lighting zone. Aurora remains outside the direct high-bandwidth path,
whose intended 4K120, VRR, HDR, eARC, and Atmos behavior must be observed
independently by the operator.

Project Aurora contributes only its existing configuration, read-only/query-only,
and bounded one-shot validation commands plus the documentation needed to record
evidence. HyperHDR continues to own capture, screen-color extraction, and the
operator-controlled live real-time color test through its supported configuration
and interface. No Project Aurora runtime adapter or continuous transmitter is
introduced.

The baseline is considered proven only when every mandatory runbook gate has
recorded operator evidence under the stated test conditions. The documentation
itself does not claim that the physical path has passed, and software cannot
verify physical LED output, direct-path video/audio properties, wiring, or
electrical safety. Multi-zone orchestration and custom runtime transmission
remain deferred to separately approved future work. See the
[single-zone baseline proof and deployment runbook](single-zone-baseline-proof.md).

## Read-only health dashboard boundary (Milestone 12)

The dashboard consumes one immutable validated `AuroraSettings` snapshot. It
reuses the existing WLED information, HyperHDR serverinfo, and non-opening V4L2
metadata validators. Its only additional device request is a fixed, bounded
WLED `GET /json/state`; it introduces no POST, DDP, capture ioctl, runtime
adapter, service operation, or power operation.

Independent collectors run concurrently, but one shared service serializes
snapshot creation and caches each result for the configured refresh interval.
Page and API requests therefore do not create overlapping hardware polls.
Collector exceptions and offline components are isolated into sanitized health
records that contain no endpoint or capture-path configuration.

Capture activity is inferred only from HyperHDR's sanitized grabber component
flag when present. The dashboard does not acquire a frame, prove frame freshness,
inspect the splitter, or claim the complete physical path is healthy. See the
[health dashboard guide](health-dashboard.md).

## Unified read-only portal boundary (Milestone 13)

Milestone 13 adds a route-aware presentation layer in front of the unchanged
Milestone 12 `HealthService`. Overview and component pages obtain one report
from that shared service and render only component-specific allowlisted fields.
No route constructs a device transport, calls a collector directly, or bypasses
the configured cache interval. Locally bundled CSS supplies responsive and
accessible presentation without a web or frontend framework, remote asset, or
browser-side device request.

The public portal accepts GET requests for native pages, its local stylesheet,
and the existing schema-version-1 health endpoint. State-changing requests to
public health routes receive method-not-allowed responses; Milestone 14's
separate login and logout boundary is described below. Unknown detail keys,
configured endpoints, capture identifiers, raw responses, exceptions,
credentials, and other installation values are not rendered. Content security,
anti-framing, no-referrer, no-sniff, and no-store response headers reinforce the
local read-only boundary without requiring TLS or breaking ordinary trusted-LAN
HTTP.

Room Map and Spatial Intelligence are descriptive preview routes only. They add
no zone model, capture analysis, AI dependency, persistence, or output path. A
future control plane must be separately authenticated, typed, bounded, audited,
and kept outside the health-snapshot service. See the
[unified portal architecture](unified-portal.md).

## Authenticated control-plane boundary (Milestone 14)

Milestone 14 adds a `ControlPlaneService` beside, not inside, the public
`HealthService`. The public portal and `GET /api/health` retain the same cached
single-flight collectors and schema-version-1 model. Authentication pages,
protected status routes, logout, and local static assets do not obtain a health
snapshot and cannot construct or invoke a hardware adapter.

Authentication configuration defaults to disabled. Disabled authentication
makes the protected boundary unavailable; it never grants access. When enabled,
credentials are verified against a bounded versioned password hash and a
thread-safe process-local service creates absolute-lifetime sessions. Cookies
contain only opaque random identifiers while the server retains identifier
digests, the bounded operator name, expiration metadata, and a per-session CSRF
token. Session and login-attempt state is memory-only and disappears on restart.

`POST /login` and CSRF-protected `POST /logout` are the only accepted POST
routes. Strict media type, length, body-size, encoding, field, and redirect
allowlists precede credential or CSRF processing. Protected HTML requests use a
login redirect, while protected API requests fail with JSON `401`. Fixed,
sanitized audit events record security outcomes without raw request, credential,
cookie, token, client, endpoint, or exception data.

The control capability contract reports mutations disabled and registers no
operations or executor. Non-executable metadata documents the required typed
input, explicit timeout, allowlisted adapter, audit, CSRF, and confirmation
boundaries for later separately reviewed operations. It cannot forward an
arbitrary URL, API path, JSON object, shell command, or device payload. No
device-control capability exists in Milestone 14. See the
[control-plane security guide](control-plane-security.md).
