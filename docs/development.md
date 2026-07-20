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
