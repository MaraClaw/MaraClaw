#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

uv run --extra dev pytest \
  --ignore=app/scripts/test_cleanup_duplicate_feishu_users.py \
  --cov-fail-under=90
