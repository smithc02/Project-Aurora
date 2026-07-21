# Changelog

## Unreleased

- Added Milestone 7 explicit Linux V4L2 query-only capability validation using
  one `VIDIOC_QUERYCAP` ioctl and no capture operations.

- Added Milestone 6 explicit, non-invasive Linux capture-device presence and
  V4L2 registration metadata validation.

- Added Milestone 5 explicit, one-shot HyperHDR read-only server-information
  validation using only GET `/json-rpc` with the fixed `serverinfo` command.

- Added Milestone 4 explicit, one-shot WLED read-only validation using only GET `/json/info`.

- Added Milestone 3 hardware-free runtime plans, component contracts,
  deterministic lifecycle coordination, health aggregation, and `aurora runtime plan`.
- Added Milestone 2 validated configuration loading with safe defaults, YAML,
  environment and CLI precedence, and a hardware-free validation command.
- Established Milestone 1 repository, development environment, documentation,
  configuration, CI, and minimal package scaffolding.
