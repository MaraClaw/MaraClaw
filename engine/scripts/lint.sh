#!/usr/bin/env bash
set -euo pipefail

# Run Ruff lint, Ruff format check, basedpyright, and ty against the repo.
# Use the dev extra so ruff/ty/basedpyright come from the locked dev dependencies.

cd "$(dirname "$0")/.."

uv run --extra dev ruff check .
uv run --extra dev ruff format --check .
uv run --extra dev basedpyright --project pyrightconfig.json app
uv run --extra dev ty check --force-exclude .
