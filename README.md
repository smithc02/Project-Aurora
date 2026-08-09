# Project Aurora

> **Status: pre-alpha.** Project Aurora is an open-source, Raspberry Pi-based
> ambient-lighting platform for a home theater. Milestones 1 through 10 establish
> validated configuration, hardware-free runtime contracts, read-only device
> information checks, bounded capture validation, and one explicit operator-only
> DDP output check. Milestone 11 defines the operator-controlled single-zone
> baseline proof and deployment runbook; it does not claim that the physical path
> has passed. Milestone 12 added a local read-only health dashboard, and
> Milestone 13 turned it into a unified, responsive portal. Milestone 14 added a
> fail-closed authenticated control-plane foundation while preserving the same
> public read-only health service and version 1 API. Milestone 15 adds only
> authenticated, explicitly enabled WLED power on, power off, and absolute
> brightness operations. Milestone 16 adds four bounded HyperHDR component-state
> operations. Milestone 17 adds CLI-only local YAML profiles, exact backups,
> atomic activation, recovery, and operator-selected rollback. Its software is
> merged and deployed, and controlled Linux filesystem validation passed; this
> does not assert that production profiles were created or activated. Milestone
> 18 implementation is in progress: its isolated production package now
> includes the strict projection and SQLite foundation plus atomic projection
> ingestion, compacted history state, monotonic sequence and bounded replay
> protection, deterministic automatic alert translation, and bounded read-only
> history, alert, and alert-event queries. No runtime entry point imports it, no
> deployment database is created, and no scheduled history, route,
> acknowledgment, retention, worker, notification, or automation behavior is
> enabled. Broader device control, browser configuration, room mapping, and
> spatial intelligence remain deferred.

## Architecture summary

The direct PS5-to-TV HDMI path remains independent from Aurora so it can retain
4K120, VRR, HDR, eARC, and Atmos. A splitter's secondary 1080p60 output feeds a
capture card and Raspberry Pi 5 running HyperHDR. HyperHDR remains the baseline
capture, color-extraction, and real-time DDP component; WLED on a QuinLED
Dig-Quad controls the LEDs. MQTT is reserved for later automation,
configuration, and telemetry, never frame data. See
[the architecture](docs/architecture.md) for the full flow.

## Initial hardware stack

- PS5
- EZCOO EZ-SP12H21 HDMI 2.1 splitter
- LG OLED C9 65-inch TV and Vizio Atmos soundbar through eARC
- Hagibis MS2130 USB 3.0 UVC capture card
- Raspberry Pi 5 running HyperHDR
- QuinLED Dig-Quad Ethernet running WLED
- BTF-LIGHTING WS2815 12 V LED strip
- Mean Well LRS-150-12 (12 V, 12.5 A, 150 W) power supply

## Development setup

Prerequisites: Git, [uv](https://docs.astral.sh/uv/), and network access for
Python 3.12 and dependency downloads. No system-wide Python installation or
sudo is required.

```bash
git clone <repository>
cd <repository>
uv sync --all-groups
uv run pytest
```

Run the complete check suite with `make check` or `./scripts/check.sh`. The
optional `./scripts/bootstrap.sh` verifies `uv`, selects Python 3.12, syncs the
environment, and validates the scaffold. See [development documentation](docs/development.md).

## Commands

```bash
uv run aurora --check
uv run aurora runtime plan --config configs/aurora.example.yaml
uv run aurora hardware validate wled --config configs/aurora.local.yaml
uv run aurora hardware validate hyperhdr --config configs/aurora.local.yaml
uv run aurora hardware validate capture-device --config configs/aurora.local.yaml
uv run aurora hardware validate capture-capability --config configs/aurora.local.yaml
uv run aurora hardware validate capture-modes --config configs/aurora.local.yaml
uv run aurora hardware validate capture-frame --config configs/aurora.local.yaml
uv run aurora hardware validate ddp-output --config configs/aurora.local.yaml
uv run aurora security hash-password
uv run aurora config profile list --profiles-dir <profiles-directory>
uv run aurora config profile validate --profiles-dir <profiles-directory> --profile maintenance
uv run aurora config profile plan --config <active-yaml> --profiles-dir <profiles-directory> --profile maintenance
uv run aurora config profile apply --config <active-yaml> --profiles-dir <profiles-directory> --backups-dir <backups-directory> --profile maintenance --confirm-apply maintenance
uv run aurora config profile backups --backups-dir <backups-directory>
uv run aurora config profile rollback --config <active-yaml> --backups-dir <backups-directory> --backup-id <generated-id> --confirm-rollback <generated-id>
uv run aurora-dashboard --config configs/aurora.local.yaml
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pytest --cov=aurora_core --cov-report=term-missing
```

## Repository layout

- `src/aurora_core/` — core package and explicit bounded validation services.
- `tests/` — package startup and packaging checks.
- `configs/` — safe example configuration and future integration notes.
- `docs/` — architecture, installation, safety, and project guidance.
- `hardware/` — conventions only; no finished wiring, CAD, or PCB artifacts.
- `scripts/` — reproducible bootstrap and validation commands.
- `.github/` — CI, issue forms, and pull-request template.

## Runtime foundation and current limitations

`aurora runtime plan` creates a sanitized immutable `RuntimePlan` from the
validated configuration snapshot. It lists the fixed order `capture_device`,
`hyperhdr`, `wled`, `ddp`, then `mqtt`; it summarizes zones and layout without
printing endpoints or credentials. “Configured” means validation supplied the
minimum descriptive fields, **not** that a device is reachable or healthy.

Future adapters will implement a narrow synchronous start/stop/health contract.
The runtime controller accepts only injected adapters, starts enabled components
in plan order, and stops successful starts in reverse order. No adapters exist
yet. Aurora has no automatic configuration reload: stop the controller, load a
new settings snapshot, build a new plan, and create a new controller.

Aurora can explicitly validate one enabled WLED device with a read-only GET
`/json/info` and an enabled HyperHDR server with one GET `/json-rpc` request
containing only `serverinfo`; neither command changes device state. Separate
operator-only capture commands provide bounded local validation. The explicit
`ddp-output` command is the sole DDP transmitter: it can submit one fixed
low-intensity RGB frame followed immediately by one blackout frame, with no
animation or retry. UDP provides no receipt acknowledgment, so this does not
prove WLED receipt, physical LED output, or the complete lighting path. See
[bounded DDP output validation](docs/ddp-output-validation.md).

No runtime adapter, continuous image processing, MQTT frame transport, system
service manipulation, or mains/power control is implemented. Configuration
validation alone does not implement or test connectivity.

Milestone 12's [read-only health dashboard](docs/health-dashboard.md) reuses the
bounded WLED, HyperHDR, and capture metadata validators, adds one fixed WLED
state GET, and collects local host metrics. Concurrent requests share one
single-flight snapshot and cannot overlap hardware polls. The public health
service remains read-only and performs no DDP, service,
capture-configuration, or power-supply mutation.

Milestone 13's [unified portal](docs/unified-portal.md) presents that same cached,
sanitized snapshot across Overview, WLED, HyperHDR, Capture, System, Room Map,
and Spatial Intelligence pages. The final two routes are explicitly inactive
future-feature previews. The existing `aurora-dashboard` command and
`GET /api/health` schema version 1 remain unchanged. The public portal pages
have no mutation handlers, persistence, frame capture, or device control.

Milestone 14's
[control-plane security foundation](docs/control-plane-security.md) adds optional
local authentication, bounded in-memory sessions, CSRF-protected logout,
attempt limiting, sanitized security audit events, and protected status routes.
Authentication is disabled by default and protected routes fail closed while it
is disabled. Login and capability requests do not poll hardware.

Milestone 15's [bounded WLED controls](docs/wled-controls.md) register exactly
`wled.power_on`, `wled.power_off`, and `wled.brightness_set`. Authentication,
the separate WLED control switch, and an operation allowlist are all required.
Fixed server routes generate fixed-shape payloads through one serialized,
rate-limited adapter and verify returned state before reporting success. No
presets, effects, colors, segments, DDP, service, power-supply, configuration,
room-zone, capture, or AI control is implemented.

Milestone 16's [bounded HyperHDR controls](docs/hyperhdr-controls.md) register
exactly `hyperhdr.video_grabber_enable`, `hyperhdr.video_grabber_disable`,
`hyperhdr.led_output_enable`, and `hyperhdr.led_output_disable`. The browser
selects only fixed routes; code generates the fixed `componentstate` payload.
One acknowledged POST is followed by one existing read-only `serverinfo` GET,
and only an exact reported Boolean match is verified. Authentication, the
separate HyperHDR control switch, and an operation allowlist are all required.
Disable routes require separate disruptive-action confirmation. There is no
generic JSON-RPC, instance, service, profile, automation, or combined ambient
control.

Milestone 17's
[local configuration profiles](docs/configuration-profiles.md) manage only the
explicitly selected YAML file through strict logical profile IDs, two-stage
validation, exact managed backups, nonblocking cross-process locking, atomic
replacement, automatic recovery, and explicit reversible rollback. Environment
and CLI overrides remain outside profiles and backups. Applying or rolling back
does not reload the running process or invoke a service manager; an external
service restart remains operator-controlled.

Milestone 11 adds no runtime behavior. Its
[single-zone baseline proof and deployment runbook](docs/single-zone-baseline-proof.md)
combines the existing validation boundaries with operator-observed direct HDMI,
HyperHDR, WLED, single-zone, stop, and recovery evidence. Only a completed
private evidence record can establish `PROVEN` under the recorded conditions;
the repository documentation does not claim the physical path has passed.

## Safety

The planned power supply has exposed mains-voltage terminals. Keep all AC work
isolated and performed by qualified people; this software is not electrical
installation guidance. Read [Safety](docs/safety.md) before handling hardware.

## Roadmap

See [the roadmap](docs/roadmap.md) for completed and planned milestones, the
[Milestone 11 single-zone baseline runbook](docs/single-zone-baseline-proof.md)
for its operator evidence requirements, and the
[Milestone 12 dashboard guide](docs/health-dashboard.md) for local deployment.
See the [Milestone 13 unified portal guide](docs/unified-portal.md) for the route
map and read-only boundary, and the
[Milestone 14 control-plane security guide](docs/control-plane-security.md) for
authentication configuration, session behavior, deployment, and recovery. See
the [Milestone 15 WLED control guide](docs/wled-controls.md) for the exact
operation, verification, activation, and rollback boundaries, and the
[Milestone 16 HyperHDR control guide](docs/hyperhdr-controls.md) for its exact
four-operation registry and two-request verification boundary. See the
[Milestone 17 configuration-profile guide](docs/configuration-profiles.md) for
the CLI, filesystem, backup, activation, and recovery boundaries. The in-progress
[Milestone 18 health-history and alerting design](docs/health-history-alerting.md)
documents the persistence, privacy, lifecycle, and bounded-automation decisions
plus the isolated storage, ingestion, and read-query slices; it does not enable
runtime behavior.

## Contributing

Read [CONTRIBUTING.md](CONTRIBUTING.md), [AGENTS.md](AGENTS.md), and relevant
documentation before making changes. Keep contributions small, configurable,
tested, and free of secrets or personal network information.

## Configuration

Aurora's configuration loader does not contact devices or test connectivity.
Settings are applied in deterministic order: command-line overrides, `AURORA_`
environment variables, an explicitly supplied YAML file, then safe built-in defaults.
Nested environment fields use `__`, for example `AURORA_WLED__ENABLED=true` and
`AURORA_LOGGING__LEVEL=DEBUG`. Dashboard authentication uses the same syntax and
is disabled by default. WLED and HyperHDR controls each have an independent
disabled-by-default switch and empty-by-default operation allowlist.

Use `aurora config validate --config path/to/aurora.yaml --log-level DEBUG` to
check an explicit file. Copy `configs/aurora.example.yaml` to an untracked file
before adding deployment-specific values. MQTT passwords use protected values
and are not printed by validation output or configuration errors. `.env.example`
documents safe environment names; Aurora deliberately does not load `.env` files.
Generate a versioned password hash interactively with
`uv run aurora security hash-password`; never pass plaintext on the command line
or commit an operator name or hash. Prefer protected process environment values
or the separately protected systemd environment file documented in the
[control-plane security guide](docs/control-plane-security.md).

Milestone 8 adds the explicit Linux-only `aurora hardware validate capture-modes`
command for bounded query-only V4L2 format, size, and interval reporting. It
opens the configured node only for that command, changes no capture
configuration, and acquires no frame. See [capture mode enumeration](docs/capture-mode-enumeration.md).

Milestone 9 adds explicit bounded read/write single-frame validation with no
streaming I/O or frame retention. See [capture-frame validation](docs/capture-frame-validation.md).

Milestone 10 adds the explicit operator-only `aurora hardware validate
ddp-output` command. It uses the configured DDP endpoint and exactly one enabled
lighting zone, sends at most one fixed low-intensity frame and one best-effort
blackout frame, and never runs automatically. It has no host, port, LED-count,
color, packet-size, destination-ID, or timeout CLI override. Runtime DDP remains
unimplemented.
