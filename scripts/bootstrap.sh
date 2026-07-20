#!/usr/bin/env bash
# Bootstrap Project Aurora on macOS or Raspberry Pi OS/Linux.
set -euo pipefail

if ! command -v uv >/dev/null 2>&1; then
  cat >&2 <<'MESSAGE'
Error: uv is required but was not found on PATH.
Install it without sudo, then re-run this script:
  curl -LsSf https://astral.sh/uv/install.sh | sh
Restart your shell if necessary so uv is on PATH.
MESSAGE
  exit 1
fi

uv python install 3.12
uv sync --all-groups
./scripts/check.sh
