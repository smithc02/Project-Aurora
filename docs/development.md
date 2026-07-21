# Development

## Setup

```bash
uv python install 3.12
uv sync --all-groups
```

`uv run` automatically uses the local `.venv`; activation is optional:

```bash
source .venv/bin/activate  # macOS/Linux
uv run aurora --check
```

## Validation

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pytest --cov=aurora_core --cov-report=term-missing
make check
```

Format deliberately with `uv run ruff format .` after reviewing the change.

## Dependency updates and lock file

Add or update dependencies in `pyproject.toml`, then resolve and test them:

```bash
uv lock
uv sync --all-groups
make check
```

Use `uv lock --upgrade` only when intentionally updating allowed dependency
versions. Commit `uv.lock` with every dependency resolution change.

## Configuration validation

The configuration loader uses safe YAML and Pydantic validation only. It does not
open capture devices or make network or hardware connections. Provide a file
explicitly with `--config`; Aurora does not search system directories. The
precedence order is CLI > `AURORA_` environment variables > YAML > built-in safe
defaults. Nested environment names use `__`, such as `AURORA_MQTT__USERNAME` and
`AURORA_WLED__PORT`.

Use an untracked configuration file for real deployment values. Passwords are
protected from repr output and are excluded from user-facing validation errors.

## Runtime-plan validation

```bash
uv run aurora runtime plan --config configs/aurora.example.yaml
```

This reuses configuration precedence (CLI > environment > YAML > defaults) and
prints a deterministic sanitized summary. It does not print endpoint details or
passwords, start components, instantiate a controller, test connectivity, or
verify hardware. Configuration snapshots are immutable runtime inputs; reload
and file watching are intentionally absent in this milestone.


## Manual WLED validation

This is an operator action, not CI activity:

```bash
uv run aurora hardware validate wled \
  --config configs/aurora.local.yaml
```

The command alone may access the configured WLED device. It uses only GET `/json/info`, does not print endpoint details, and sends no LED data or state changes. `--timeout` overrides environment, YAML, and the 2.0 second default.

## Manual HyperHDR validation

```bash
uv run aurora hardware validate hyperhdr \
  --config configs/aurora.local.yaml
```

This operator action is not run in CI. It alone may access the configured
HyperHDR host, using exactly one GET `/json-rpc` request whose internally
URL-encoded command is `serverinfo`. `--timeout` overrides
`AURORA_HYPERHDR__VALIDATION_TIMEOUT_SECONDS`, YAML, and the 2.0-second
default. It rejects redirects, accepts at most 256 KiB, prints no endpoint
details, and changes no HyperHDR state.

## Manual V4L2 capability validation

```bash
uv run aurora hardware validate capture-capability \
  --config configs/aurora.local.yaml
```

This Linux-only manual action uses V4L2-required `O_RDWR`, `O_NONBLOCK`, and
`O_CLOEXEC` when available, makes one `VIDIOC_QUERYCAP` request, then closes the
descriptor. It does not read a frame, configure a format, allocate buffers, or
start streaming.


## Manual V4L2 mode enumeration

```bash
uv run aurora hardware validate capture-modes --config configs/aurora.local.yaml
```

This Linux-only command is manual, query-only, bounded, and non-streaming. It
opens the configured node once, makes one capability query and fixed enumeration
ioctls only, closes it, changes no capture setting, and acquires no frame.
