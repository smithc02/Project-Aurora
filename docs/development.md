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
