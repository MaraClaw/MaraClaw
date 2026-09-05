#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

ENV_FILE="${ENV_FILE:-.env}"
if [ -f "$ENV_FILE" ]; then
    echo "[openclaw-build] Sourcing $ENV_FILE"
    set -a
    # shellcheck disable=SC1090
    . "$ENV_FILE"
    set +a
fi

TENCENTDB_PLUGIN_VERSION="${TENCENTDB_PLUGIN_VERSION:-1.0.1}"
OPENCLAW_VERSION="${OPENCLAW_VERSION:-2026.9.1}"
OPENCLAW_SHA256="${OPENCLAW_SHA256:-sha256:1bfcac877d53f1e41b69d15c24e081895b2f07d6ff2ffdfe0bf8a7336ab00e59}"
GOGCLI_VERSION="${GOGCLI_VERSION:-0.39.0}"
GOGCLI_SHA256="${GOGCLI_SHA256:-sha256:040984e38291da2f23ddeefbd67371c5bf32de6be3177d4f5a816f7fe51bacb7}"
DOCKER_PLATFORM="${DOCKER_PLATFORM:-linux/arm64}"

if [ "$DOCKER_PLATFORM" != "linux/arm64" ]; then
    echo "[openclaw-build] gogcli is pinned to the linux/arm64 release; set DOCKER_PLATFORM=linux/arm64 or add per-arch gogcli artifacts before building $DOCKER_PLATFORM." >&2
    exit 2
fi

echo "[openclaw-build] Building openclaw:local for $DOCKER_PLATFORM with OpenClaw $OPENCLAW_VERSION and TencentDB plugin $TENCENTDB_PLUGIN_VERSION"
docker buildx build \
    --platform "$DOCKER_PLATFORM" \
    -f Dockerfile.openclaw \
    --build-arg TENCENTDB_PLUGIN_VERSION="$TENCENTDB_PLUGIN_VERSION" \
    --build-arg OPENCLAW_VERSION="$OPENCLAW_VERSION" \
    --build-arg OPENCLAW_SHA256="$OPENCLAW_SHA256" \
    --build-arg GOGCLI_VERSION="$GOGCLI_VERSION" \
    --build-arg GOGCLI_SHA256="$GOGCLI_SHA256" \
    -t openclaw:local \
    --load \
    .
echo "[openclaw-build] Successfully built local Docker image openclaw:local"
