# Unified Project Aurora portal

Milestone 13 converts the Milestone 12 health dashboard into Project Aurora's
responsive landing portal. It is a presentation and architecture milestone: the
portal makes the existing sanitized health snapshot easier to navigate while
remaining dependency-free, local, and completely read-only. Milestone 13 itself
was unauthenticated; Milestone 14 later adds a separate optional protected
status boundary without changing these public pages. Milestone 15 later adds a
separately enabled protected WLED page while the Milestone 13 public portal
remains read-only.

Run it with the existing command:

```bash
uv run aurora-dashboard --config <config-file>
```

The configuration model, cache interval, bind behavior, service deployment, and
`GET /api/health` schema version 1 remain backward compatible. Existing clients
and installations require no configuration change.

## Route map

| Route | Purpose |
| --- | --- |
| `/` | Overview of overall, component, observed output, and host health. |
| `/wled` | Sanitized WLED health and currently observed output state. |
| `/hyperhdr` | Sanitized HyperHDR instance, grabber, HDR, and LED-output state. |
| `/capture` | Non-opening capture-device metadata and inferred grabber activity. |
| `/system` | Raspberry Pi resource health, host uptime, and service uptime. |
| `/room-map` | Inactive preview of the future virtual-room model. |
| `/spatial-intelligence` | Inactive preview of the future spatial-event pipeline. |
| `/api/health` | Unchanged machine-readable health response, schema version 1. |
| `/static/portal.css` | Locally bundled portal presentation. |
| `/controls/wled` | Authenticated Milestone 15 WLED status and bounded forms; unavailable when authentication is disabled. |

Milestone 14 adds separate authentication routes at `GET`/`POST /login` and
`POST /logout`, plus protected status routes at `GET /controls` and
`GET /api/control/status`. When authentication is enabled, the portal shell
renders Login or Controls navigation according to server-side session state.
When it is disabled, those protected routes remain unavailable.
Milestone 15 adds three fixed POST routes under `/controls/wled`; they are not
part of the public portal route group.

Every HTML page has a semantic header, primary navigation, current page title,
overall health indicator, component status where applicable, snapshot time,
service uptime, keyboard focus treatment, and a skip link. CSS adapts the page
layout for smaller screens. The portal uses no remote font, CDN, analytics,
tracker, externally hosted asset, frontend framework, or JavaScript.

## Read-only snapshot boundary

All pages consume the same `HealthService` instance used by Milestone 12. That
service serializes collector sweeps and caches the resulting immutable
`HealthReport` for `dashboard.refresh_seconds`. A page handler asks for that
single report once. It does not invoke WLED, HyperHDR, capture, or system
collectors directly, and it does not make a page-specific device request.

This preserves the existing single-flight guarantee: simultaneous page and API
requests cannot overlap hardware polls. Moving among pages during a cache
interval reuses the same snapshot. Individual component failures remain isolated
and render as degraded or unavailable states; one offline component cannot take
the portal or another component page offline.

The Milestone 13 portal added no state-changing operation. Milestone 14 accepted
POST only for login and CSRF-protected logout in a separate authentication
boundary. Milestone 15 adds only the three protected, route-specific WLED POST
handlers documented in [bounded WLED controls](wled-controls.md); every other
unsupported mutation method still returns `405 Method Not Allowed`. There are
no arbitrary JSON inputs, configuration writes, service commands, power-supply
commands, DDP output, frame capture, persistence, or outbound internet access.

## Sanitized-data policy

The JSON endpoint continues to return the existing bounded health model. HTML
rendering applies an additional component-specific allowlist. Only the known
WLED, HyperHDR, capture, and system fields documented by the health collectors
can appear. All dynamic labels and values are HTML escaped.

The portal never renders configured hosts, IP addresses, ports, URLs,
credentials, capture identifiers or paths, raw device responses, raw
exceptions, MAC addresses, or unknown detail fields. The capture page displays
only a bounded device name and validation booleans; viewing it neither opens the
device nor acquires a frame. Security headers restrict content to local styles,
prevent framing, disable referrer data, prevent MIME sniffing, and disable
caching of health responses.

The server intentionally does not implement TLS. Keep it on the local host or a
trusted, firewall-controlled LAN as documented in the health-dashboard guide.

## Control-plane boundary after Milestones 14 and 15

Health collection and presentation are not a control plane. Milestone 14 creates
a separate fail-closed authenticated foundation with process-local sessions,
CSRF-protected logout, attempt limiting, and sanitized audit events. It exposes
only protected status pages and registered no mutation in that milestone.
Authentication and status requests do not use the health snapshot or contact a
device. See the
[control-plane security guide](control-plane-security.md).

Milestone 15 registers only WLED power on, power off, and bounded absolute
brightness. Those operations require the existing authenticated session and
CSRF checks plus a separate control switch and explicit allowlist. The protected
WLED GET page uses the shared health snapshot; capability, login, logout, and
mutation requests do not poll health. No public health page mutates a device.

Future state-changing controls must remain inside that boundary and must not
turn the health snapshot API into a mutation or forwarding channel. Before an
operation can be introduced, the design requires at minimum:

- local authentication;
- protected sessions;
- CSRF protection;
- strict typed input validation;
- bounded API operations;
- audit logging;
- confirmation for disruptive actions;
- configuration backup and rollback where supported;
- no arbitrary API forwarding;
- no arbitrary JSON forwarding; and
- no unrestricted shell execution.

Future controls must also reuse existing configuration and validation contracts
where practical, impose explicit value and time bounds, redact audit data, test
partial failure and rollback behavior, and document device-specific safety
assumptions. Authentication alone is not authorization to expose a device's raw
API. WLED and HyperHDR control milestones require independent review.

## Planned room mapping and spatial intelligence

The planned room model will represent named LED zones, their validated logical
relationships, orientation, capabilities, and bounded output limits. Deployment
values remain configuration, not code or browser-visible metadata. The Room Map
page currently creates no model and controls no zone.

A later spatial-intelligence layer may analyze approved frames, track motion or
objects, estimate a trajectory and screen-edge exit, and emit a typed spatial
event. It must not emit raw LED commands. A separate deterministic effects
engine will validate confidence, timing, duration, brightness, rate, and allowed
zones before producing any output. Low-confidence events will be suppressed or
fall back to normal ambient lighting.

Development must begin with recorded clips and a visual debug overlay. Physical
multi-zone output comes only after the analysis and event contracts are proven.
Initial learning may tune bounded calibration and user-preference parameters; it
must not rewrite control logic. Standard ambient operation remains the fallback.
See the [roadmap](roadmap.md) for the staged Milestone 14–25 progression and a
worked spatial-continuation example.

## Milestone 13 non-goals and later status

Milestone 13 does not implement authentication, sessions, CSRF tokens, device
controls, WLED configuration, HyperHDR mutations, DDP transmission, service
management, power control, persistence, health history, alerting, automation,
TLS, remote access, frame capture, computer vision, AI, room calibration,
multi-zone orchestration, spatial effects, or game/content profiles. Preview
pages communicate architectural intent only; they are not feature stubs or
control surfaces.

Milestone 14 now supplies authentication, sessions, CSRF-protected logout, and
mutation-safety contracts, but none of the device, service, output, persistence,
room, or AI capabilities deferred by Milestone 13. The public portal routes and
health snapshot remain read-only and backward compatible.

Milestone 15 later adds only the separately protected WLED operation subset.
It does not alter the historical Milestone 13 scope or implement the other
deferred controls, room model, or spatial-intelligence features.
