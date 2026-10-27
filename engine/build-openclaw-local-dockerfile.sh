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

LOCAL_IMAGE="${MARACLAW_OPENCLAW_IMAGE:-openclaw:local}"
OPENCLAW_VERSION="${OPENCLAW_VERSION:-2026.10.7}"
OPENCLAW_SHA256="${OPENCLAW_SHA256:-sha256:45dc6dadab3ca4e5286262226d6b61554309435a5e83b5d25c803a3a12ef2f81}"
GOGCLI_VERSION="${GOGCLI_VERSION:-0.47.0}"
GOGCLI_AMD64_SHA256="${GOGCLI_AMD64_SHA256:-sha256:c3ed94efca90c754cdda300e3c7f8cc2f3d7df86d3fdc5d11f996c7518a09c24}"
GOGCLI_ARM64_SHA256="${GOGCLI_ARM64_SHA256:-sha256:90d236ab91c6dae7139bfe0508eab94a8b4323238b62fa29e6cf479a352576f2}"
DOCKER_PLATFORM="${DOCKER_PLATFORM:-linux/arm64}"

case "$DOCKER_PLATFORM" in
    linux/amd64|linux/arm64) ;;
    *)
        echo "[openclaw-build] DOCKER_PLATFORM must select exactly one supported target: linux/amd64 or linux/arm64 (got '$DOCKER_PLATFORM')." >&2
        exit 2
        ;;
esac

echo "[openclaw-build] Building $LOCAL_IMAGE for $DOCKER_PLATFORM with OpenClaw $OPENCLAW_VERSION"
docker buildx build \
    --platform "$DOCKER_PLATFORM" \
    -f Dockerfile.openclaw \
    --build-arg OPENCLAW_VERSION="$OPENCLAW_VERSION" \
    --build-arg OPENCLAW_SHA256="$OPENCLAW_SHA256" \
    --build-arg GOGCLI_VERSION="$GOGCLI_VERSION" \
    --build-arg GOGCLI_AMD64_SHA256="$GOGCLI_AMD64_SHA256" \
    --build-arg GOGCLI_ARM64_SHA256="$GOGCLI_ARM64_SHA256" \
    -t "$LOCAL_IMAGE" \
    --load \
    .
echo "[openclaw-build] Successfully built local Docker image $LOCAL_IMAGE"
