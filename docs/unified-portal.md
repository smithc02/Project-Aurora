# Unified Project Aurora portal

Milestone 13 converts the Milestone 12 health dashboard into Project Aurora's
responsive landing portal. It is a presentation and architecture milestone: the
portal makes the existing sanitized health snapshot easier to navigate while
remaining unauthenticated, dependency-free, local, and completely read-only.

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

The portal adds no POST, PUT, PATCH, or DELETE operation. Those methods return
`405 Method Not Allowed` and do not poll hardware. There are no forms, mutation
endpoints, arbitrary JSON inputs, configuration writes, service commands, power
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

## Future control-plane boundary

Health collection and presentation are not a control plane. Future
state-changing controls must be designed as a separate authenticated boundary
and must not turn the health snapshot API into a mutation or forwarding channel.
Before any mutation can be introduced, the design requires at minimum:

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

## Milestone 13 non-goals

Milestone 13 does not implement authentication, sessions, CSRF tokens, device
controls, WLED configuration, HyperHDR mutations, DDP transmission, service
management, power control, persistence, health history, alerting, automation,
TLS, remote access, frame capture, computer vision, AI, room calibration,
multi-zone orchestration, spatial effects, or game/content profiles. Preview
pages communicate architectural intent only; they are not feature stubs or
control surfaces.
