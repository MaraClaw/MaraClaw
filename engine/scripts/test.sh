#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

uv run --extra dev pytest --cov-fail-under=90
