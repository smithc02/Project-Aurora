# Instructions for coding agents

- Read `README.md` and relevant documentation before modifying code.
- Keep all hardware-specific values configurable. Never assume an LED count, IP
  address, GPIO pin, or capture-device path.
- Do not add secrets, credentials, personal network data, or private addresses.
- Use DDP for real-time LED frames; reserve MQTT for automation and telemetry.
- Add or update tests with functional changes and update documentation whenever
  the architecture changes.
- Do not add mains-power control code unless a separately approved milestone
  explicitly authorizes it.
- Do not replace HyperHDR before baseline hardware operation is proven.
- Work in small, reviewable milestones. Stop and report ambiguity rather than
  inventing safety-critical details.
