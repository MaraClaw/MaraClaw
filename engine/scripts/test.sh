#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

uv run --extra dev pytest \
  --cov=app \
  --cov-config=scripts/coverage-gate.toml \
  --cov-report=term-missing:skip-covered \
  --cov-fail-under=90
