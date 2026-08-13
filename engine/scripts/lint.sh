#!/usr/bin/env bash
set -euo pipefail

# Run Ruff lint, Ruff format check, and ty type-check against the repo.
# Use the dev extra so ruff/ty come from the locked dev dependencies.

cd "$(dirname "$0")/.."

uv run --extra dev ruff check .
uv run --extra dev ruff format --check .
uv run --extra dev ty check --force-exclude .
