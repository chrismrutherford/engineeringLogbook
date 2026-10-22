#!/usr/bin/env bash
# Launch the engineer-logbook terminal UI (read-only viewer, Postgres backend).
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_DIR"

if ! command -v uv >/dev/null 2>&1; then
  echo "error: uv is required (https://docs.astral.sh/uv/)" >&2
  exit 1
fi

# First run: create the venv and install dependencies.
if [ ! -d .venv ]; then
  echo "first run: installing dependencies..."
  uv sync --quiet
fi

exec uv run --quiet engineer-logbook-ui "$@"
