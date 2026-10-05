#!/usr/bin/env bash
# Build-time adaptation of the cached upstream TencentDB patch script:
# OpenClaw 2026.9.8 ships hook bundles as dist/*.mjs, but the upstream
# 1.0.1 candidate discovery greps only dist/*.js. Rewrite that one exact
# discovery line to also include '*.mjs', failing closed if the upstream
# source drifts from the pinned shape.
set -eu

PATCH_SCRIPT="$1"

fail() {
    printf '[tencentdb-patch-adapt] %s\n' "$1" >&2
    exit 1
}

[[ -f "$PATCH_SCRIPT" && ! -L "$PATCH_SCRIPT" ]] || fail "patch script missing or not a regular file"

UPSTREAM_DISCOVERY="mapfile -t CANDIDATE_FILES < <(grep -rl 'after_tool_call' \"\$DIST_DIR\" --include='*.js' 2>/dev/null || true)"
ADAPTED_DISCOVERY="mapfile -t CANDIDATE_FILES < <(grep -rl 'after_tool_call' \"\$DIST_DIR\" --include='*.js' --include='*.mjs' 2>/dev/null || true)"

occurrences="$(grep -cxF "$UPSTREAM_DISCOVERY" "$PATCH_SCRIPT" || true)"
[[ "$occurrences" == 1 ]] || fail "expected exactly one upstream discovery line, found $occurrences"

adapted="$(awk -v old="$UPSTREAM_DISCOVERY" -v new="$ADAPTED_DISCOVERY" '$0 == old { print new; next } { print }' "$PATCH_SCRIPT")"
printf '%s\n' "$adapted" > "$PATCH_SCRIPT"
