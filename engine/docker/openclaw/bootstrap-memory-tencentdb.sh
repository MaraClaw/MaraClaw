#!/usr/bin/env sh
set -eu

STATE_DIR="${OPENCLAW_STATE_DIR:-/home/node/.openclaw}"
PLUGIN_VERSION="${TENCENTDB_PLUGIN_VERSION:-0.3.6}"
PLUGIN_PACKAGE="@tencentdb-agent-memory/memory-tencentdb"
PLUGIN_ARCHIVE="${OPENCLAW_MEMORY_TENCENTDB_ARCHIVE:-/opt/openclaw-plugin-cache/memory-tencentdb.tgz}"
MEMORY_ENABLED="${OPENCLAW_MEMORY_TENCENTDB_ENABLED:-true}"
MARKER="$STATE_DIR/.bootstrap-tencentdb-version"

if [ "$#" -eq 0 ]; then
    set -- openclaw gateway
fi

case "$MEMORY_ENABLED" in
    false|False|FALSE|0|no|No|NO)
        exec "$@"
        ;;
esac

mkdir -p "$STATE_DIR"

INSTALLED_VERSION=""
if [ -f "$MARKER" ]; then
    INSTALLED_VERSION="$(cat "$MARKER")"
fi

if [ "$INSTALLED_VERSION" != "$PLUGIN_VERSION" ]; then
    if [ -f "$PLUGIN_ARCHIVE" ]; then
        if [ ! -f "$PLUGIN_ARCHIVE.sha256" ]; then
            echo "[openclaw-bootstrap] Missing checksum: $PLUGIN_ARCHIVE.sha256" >&2
            exit 1
        fi
        sha256sum -c "$PLUGIN_ARCHIVE.sha256" >&2
        SOURCE="$PLUGIN_ARCHIVE"
    else
        SOURCE="$PLUGIN_PACKAGE@$PLUGIN_VERSION"
    fi

    echo "[openclaw-bootstrap] Installing memory-tencentdb plugin from $SOURCE" >&2
    openclaw plugins install "$SOURCE" --pin
    printf '%s' "$PLUGIN_VERSION" > "$MARKER"
    echo "[openclaw-bootstrap] memory-tencentdb plugin bootstrap complete" >&2
fi

exec "$@"
