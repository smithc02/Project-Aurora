#!/usr/bin/env bash
# Run the complete local validation suite.
set -euo pipefail

uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pytest --cov=aurora_core --cov-report=term-missing
