#!/usr/bin/env bash
# Prepare deploy/.env and host paths as described in deploy/README.md before running.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"
exec python3 -m deploy.starter "$@"
