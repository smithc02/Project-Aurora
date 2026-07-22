# Project Aurora

> **Status: pre-alpha.** Project Aurora is an open-source, Raspberry Pi-based
> ambient-lighting platform for a home theater. Milestones 1 through 10 establish
> validated configuration, hardware-free runtime contracts, read-only device
> information checks, bounded capture validation, and one explicit operator-only
> DDP output check. Milestone 11 defines the operator-controlled single-zone
> baseline proof and deployment runbook; it does not claim that the physical path
> has passed. Runtime lighting control remains deferred.

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

See [the roadmap](docs/roadmap.md) for completed and planned milestones, and the
[Milestone 11 single-zone baseline runbook](docs/single-zone-baseline-proof.md)
for its operator evidence requirements.

## Contributing

Read [CONTRIBUTING.md](CONTRIBUTING.md), [AGENTS.md](AGENTS.md), and relevant
documentation before making changes. Keep contributions small, configurable,
tested, and free of secrets or personal network information.

## Configuration

Aurora's configuration loader does not contact devices or test connectivity.
Settings are applied in deterministic order: command-line overrides, `AURORA_`
environment variables, an explicitly supplied YAML file, then safe built-in defaults.
Nested environment fields use `__`, for example `AURORA_WLED__ENABLED=true` and
`AURORA_LOGGING__LEVEL=DEBUG`.

Use `aurora config validate --config path/to/aurora.yaml --log-level DEBUG` to
check an explicit file. Copy `configs/aurora.example.yaml` to an untracked file
before adding deployment-specific values. MQTT passwords use protected values
and are not printed by validation output or configuration errors. `.env.example`
documents safe environment names; Aurora deliberately does not load `.env` files.

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
