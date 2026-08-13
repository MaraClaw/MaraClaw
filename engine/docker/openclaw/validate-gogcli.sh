#!/bin/sh
set -eu

if [ -z "${GOG_KEYRING_PASSWORD_FILE:-}" ]; then
    exec "$@"
fi

if [ ! -f "$GOG_KEYRING_PASSWORD_FILE" ]; then
    printf '%s\n' "GOG_KEYRING_PASSWORD_FILE does not exist: $GOG_KEYRING_PASSWORD_FILE" >&2
    exit 1
fi

if [ -z "${GOG_HOME:-}" ]; then
    printf '%s\n' "GOG_HOME must be set when GOG_KEYRING_PASSWORD_FILE is used" >&2
    exit 1
fi

GOG_KEYRING_PASSWORD=$(cat "$GOG_KEYRING_PASSWORD_FILE")
export GOG_KEYRING_PASSWORD

if [ "${GOG_HOME:-}" ]; then
    mkdir -p "$GOG_HOME"
    chmod 0700 "$GOG_HOME"
fi

if [ ! -f "${GOG_HOME:?GOG_HOME must be set}/.gog-version-checked" ]; then
    gog --version
    touch "$GOG_HOME/.gog-version-checked"
fi

exec "$@"
