#!/usr/bin/env bash
# Build Dockerfile.openclaw and push a version-tagged image to Docker Hub.
#
# Build and push linux/amd64 and linux/arm64 together as a manifest:
#   ${DOCKERHUB_NAMESPACE}/openclaw:${IMAGE_TAG}
#
# Usage:
#   DOCKERHUB_NAMESPACE=youruser ./publish-openclaw-local-dockerfile.sh
#   OPENCLAW_PUBLISH_IMAGE=youruser/openclaw IMAGE_TAG=2026.10.7 ./publish-openclaw-local-dockerfile.sh
#   PUSH_LATEST=1 DOCKERHUB_NAMESPACE=youruser ./publish-openclaw-local-dockerfile.sh
#
# Environment (set in .env or on the command line):
#   DOCKERHUB_NAMESPACE     Docker Hub user or org (required unless OPENCLAW_PUBLISH_IMAGE is set)
#   OPENCLAW_PUBLISH_IMAGE  full repo name          (default: ${DOCKERHUB_NAMESPACE}/openclaw)
#   IMAGE_TAG               Hub tag                 (default: $OPENCLAW_VERSION)
#   PUSH_LATEST=1           also tag and push :latest
#   OPENCLAW_VERSION, OPENCLAW_SHA256, GOGCLI_VERSION, GOGCLI_{AMD64,ARM64}_SHA256
#                           same pins as the local build script
#
# Requires an existing `docker login` to Docker Hub. This script does not store credentials.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

ENV_FILE="${ENV_FILE:-.env}"
if [ -f "$ENV_FILE" ]; then
    echo "[openclaw-publish] Sourcing $ENV_FILE"
    set -a
    # shellcheck disable=SC1090
    . "$ENV_FILE"
    set +a
fi

OPENCLAW_VERSION="${OPENCLAW_VERSION:-2026.10.7}"
OPENCLAW_SHA256="${OPENCLAW_SHA256:-sha256:45dc6dadab3ca4e5286262226d6b61554309435a5e83b5d25c803a3a12ef2f81}"
GOGCLI_VERSION="${GOGCLI_VERSION:-0.47.0}"
GOGCLI_AMD64_SHA256="${GOGCLI_AMD64_SHA256:-sha256:c3ed94efca90c754cdda300e3c7f8cc2f3d7df86d3fdc5d11f996c7518a09c24}"
GOGCLI_ARM64_SHA256="${GOGCLI_ARM64_SHA256:-sha256:90d236ab91c6dae7139bfe0508eab94a8b4323238b62fa29e6cf479a352576f2}"
IMAGE_TAG="${IMAGE_TAG:-$OPENCLAW_VERSION}"

if [ -n "${OPENCLAW_PUBLISH_IMAGE:-}" ]; then
    PUBLISH_IMAGE="$OPENCLAW_PUBLISH_IMAGE"
elif [ -n "${DOCKERHUB_NAMESPACE:-}" ]; then
    PUBLISH_IMAGE="${DOCKERHUB_NAMESPACE}/openclaw"
else
    echo "[openclaw-publish] ERROR: set DOCKERHUB_NAMESPACE (Docker Hub user/org) or OPENCLAW_PUBLISH_IMAGE (e.g. youruser/openclaw)" >&2
    exit 2
fi

case "$PUBLISH_IMAGE" in
    */*) ;;
    *)
        echo "[openclaw-publish] ERROR: OPENCLAW_PUBLISH_IMAGE must be namespace/name (got '$PUBLISH_IMAGE')" >&2
        exit 2
        ;;
esac

if printf '%s' "$PUBLISH_IMAGE" | grep -q '[[:upper:][:space:]]'; then
    echo "[openclaw-publish] ERROR: Docker Hub repository '$PUBLISH_IMAGE' must be lowercase with no spaces" >&2
    exit 2
fi

if ! printf '%s' "$IMAGE_TAG" | grep -Eq '^[A-Za-z0-9_][A-Za-z0-9_.-]*$'; then
    echo "[openclaw-publish] ERROR: IMAGE_TAG '$IMAGE_TAG' is not a valid Docker tag" >&2
    exit 2
fi

VERSIONED_REF="${PUBLISH_IMAGE}:${IMAGE_TAG}"
echo "[openclaw-publish] Building linux/amd64,linux/arm64 and pushing $VERSIONED_REF"
TAGS=(-t "$VERSIONED_REF")

if [ "${PUSH_LATEST:-0}" = "1" ]; then
    LATEST_REF="${PUBLISH_IMAGE}:latest"
    TAGS+=(-t "$LATEST_REF")
fi

docker buildx build \
    --platform linux/amd64,linux/arm64 \
    -f Dockerfile.openclaw \
    --build-arg OPENCLAW_VERSION="$OPENCLAW_VERSION" \
    --build-arg OPENCLAW_SHA256="$OPENCLAW_SHA256" \
    --build-arg GOGCLI_VERSION="$GOGCLI_VERSION" \
    --build-arg GOGCLI_AMD64_SHA256="$GOGCLI_AMD64_SHA256" \
    --build-arg GOGCLI_ARM64_SHA256="$GOGCLI_ARM64_SHA256" \
    "${TAGS[@]}" \
    --push \
    .

echo "[openclaw-publish] Published $VERSIONED_REF"
