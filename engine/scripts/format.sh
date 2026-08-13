#!/usr/bin/env bash
set -euo pipefail

# Apply Ruff lint fixes and then run the Ruff formatter across the repo.
# Use the dev extra so ruff comes from the locked dev dependencies.

cd "$(dirname "$0")/.."

uv run --extra dev ruff check . --fix
uv run --extra dev ruff format .
