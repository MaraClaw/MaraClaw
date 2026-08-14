#!/usr/bin/env bash
# Build Dockerfile.openclaw and push a version-tagged image to Docker Hub.
#
# This wraps build-openclaw-local-dockerfile.sh so the local openclaw:local
# tag stays in sync, then tags and pushes:
#   ${DOCKERHUB_NAMESPACE}/openclaw:${IMAGE_TAG}
#
# Usage:
#   DOCKERHUB_NAMESPACE=youruser ./publish-openclaw-local-dockerfile.sh
#   OPENCLAW_PUBLISH_IMAGE=youruser/openclaw IMAGE_TAG=2026.7.1-2 ./publish-openclaw-local-dockerfile.sh
#   PUSH_LATEST=1 DOCKERHUB_NAMESPACE=youruser ./publish-openclaw-local-dockerfile.sh
#
# Environment (set in .env or on the command line):
#   DOCKERHUB_NAMESPACE     Docker Hub user or org (required unless OPENCLAW_PUBLISH_IMAGE is set)
#   OPENCLAW_PUBLISH_IMAGE  full repo name          (default: ${DOCKERHUB_NAMESPACE}/openclaw)
#   IMAGE_TAG               Hub tag                 (default: $OPENCLAW_VERSION)
#   PUSH_LATEST=1           also tag and push :latest
#   OPENCLAW_VERSION, OPENCLAW_SHA256, GOGCLI_*, TENCENTDB_PLUGIN_VERSION, DOCKER_PLATFORM
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

OPENCLAW_VERSION="${OPENCLAW_VERSION:-2026.7.1-2}"
DOCKER_PLATFORM="${DOCKER_PLATFORM:-linux/arm64}"
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

if [ "$DOCKER_PLATFORM" != "linux/arm64" ]; then
    echo "[openclaw-publish] gogcli is pinned to the linux/arm64 release; set DOCKER_PLATFORM=linux/arm64 or add per-arch gogcli artifacts before building $DOCKER_PLATFORM." >&2
    exit 2
fi

VERSIONED_REF="${PUBLISH_IMAGE}:${IMAGE_TAG}"
echo "[openclaw-publish] Building and pushing $VERSIONED_REF (also tagging openclaw:local)"

"$SCRIPT_DIR/build-openclaw-local-dockerfile.sh"

docker tag openclaw:local "$VERSIONED_REF"
echo "[openclaw-publish] Pushing $VERSIONED_REF"
docker push "$VERSIONED_REF"

if [ "${PUSH_LATEST:-0}" = "1" ]; then
    LATEST_REF="${PUBLISH_IMAGE}:latest"
    docker tag openclaw:local "$LATEST_REF"
    echo "[openclaw-publish] Pushing $LATEST_REF"
    docker push "$LATEST_REF"
fi

echo "[openclaw-publish] Published $VERSIONED_REF"
