#!/usr/bin/env bash
# Prepare deploy/.env and host paths as described in deploy/README.md before running.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE=(docker compose
    --project-directory "$SCRIPT_DIR"
    --env-file "$SCRIPT_DIR/deploy/.env"
    -f "$SCRIPT_DIR/compose.yaml")

"${COMPOSE[@]}" config --quiet
exec "${COMPOSE[@]}" up -d --build
