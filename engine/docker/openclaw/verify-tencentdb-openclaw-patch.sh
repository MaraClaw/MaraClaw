#!/usr/bin/env bash
set -eu

OPENCLAW_ROOT="$1"
PATCH_SCRIPT="$2"
DIST_DIR="$OPENCLAW_ROOT/dist"
CLASSIFIER="/usr/local/bin/classify-tencentdb-openclaw-hook.cjs"

fail() {
    local stage="$1"
    local code="$2"
    case "$stage:$code" in
        candidate:no-target|patch:command-failed|verify:target-untransformed|verify:non-target-drift|backup:missing-or-invalid|backup:digest-mismatch|idempotence:target-drift|idempotence:backup-drift)
            ;;
        *)
            stage=internal
            code=unclassified
            ;;
    esac
    printf '[tencentdb-patch-verify] stage=%s code=%s\n' "$stage" "$code" >&2
    exit 1
}

[[ -x "$PATCH_SCRIPT" ]] || fail internal unclassified
[[ -d "$DIST_DIR" ]] || fail internal unclassified

VERIFY_DIR="$(mktemp -d)"
trap 'rm -rf "$VERIFY_DIR"' EXIT

targets=()
target_digests=()
target_states=()
target_backup_states=()
target_backup_digests=()
after_target_digests=()
after_backup_states=()
after_backup_digests=()
non_targets=()
non_target_digests=()

is_exact_target() {
    local target="$1"
    if [[ "$(basename "$target")" == dispatch-*.js ]] \
        && perl -0ne 'exit 0 if /after_tool_call[\s\S]{0,2000}durationMs/; exit 1' "$target" \
        && grep -qP '^\s+durationMs\s*$' "$target"; then
        return 0
    fi
    perl -0ne '
        exit 0 if /after_tool_call[\s\S]{0,2000}durationMs\s*\n\s*\};\s*\n\s*(?:hookRunnerAfter|await\s+\S*hookRunner\S*\.runAfterToolCall|hookRunner\S*\.runAfterToolCall)/;
        exit 0 if /after_tool_call[\s\S]{0,800}durationMs\s*\n\s*\};/;
        exit 0 if /after_tool_call[\s\S]{0,2000}?(?:hookEvent|hook_event)[\s\S]{0,500}?durationMs\s*\n\s*\};/;
        exit 1;
    ' "$target" 2>/dev/null
}

has_injected_messages() {
    local classifier="$CLASSIFIER"
    if [[ ! -e "$classifier" && ! -L "$classifier" ]]; then
        local verifier_dir
        if ! verifier_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" 2>/dev/null && pwd -P)"; then
            fail internal unclassified
        fi
        classifier="$verifier_dir/classify-tencentdb-openclaw-hook.cjs"
    fi
    [[ -f "$classifier" && ! -L "$classifier" ]] || fail internal unclassified
    local classification="$VERIFY_DIR/classification"
    if ! node --expose-internals "$classifier" "$1" > "$classification" 2>/dev/null; then
        fail internal unclassified
    fi
    if printf 'prepatched\n' | cmp -s - "$classification"; then
        return 0
    fi
    if printf 'unpatched\n' | cmp -s - "$classification"; then
        return 1
    fi
    fail internal unclassified
}

sha256() {
    sha256sum "$1" 2>/dev/null | cut -d " " -f 1
}

while IFS= read -r -d "" candidate; do
    if is_exact_target "$candidate"; then
        targets+=("$candidate")
        target_digests+=("$(sha256 "$candidate")")
        if has_injected_messages "$candidate"; then
            target_states+=("prepatched")
        else
            target_states+=("unpatched")
        fi
        backup="${candidate}.pre-offload-patch.bak"
        if [[ ! -e "$backup" && ! -L "$backup" ]]; then
            target_backup_states+=("absent")
            target_backup_digests+=("")
        elif [[ -f "$backup" && ! -L "$backup" ]]; then
            target_backup_states+=("regular")
            target_backup_digests+=("$(sha256 "$backup")")
        else
            fail backup missing-or-invalid
        fi
        printf '%s\t%s\n' "$candidate" "$(sha256 "$candidate")" >> "$VERIFY_DIR/targets"
    else
        non_targets+=("$candidate")
        non_target_digests+=("$(sha256 "$candidate")")
    fi
done < <(find "$DIST_DIR" -type f -name "*.js" -print0 2>/dev/null)

if (( ${#targets[@]} == 0 )); then
    fail candidate no-target
fi

if ! bash "$PATCH_SCRIPT" "$OPENCLAW_ROOT" >"$VERIFY_DIR/patch-output" 2>&1; then
    fail patch command-failed
fi

for index in "${!targets[@]}"; do
    target="${targets[$index]}"
    backup="${target}.pre-offload-patch.bak"
    has_injected_messages "$target" || fail verify target-untransformed
    target_digest="$(sha256 "$target")"
    case "${target_states[$index]}" in
        prepatched)
            [[ "$target_digest" == "${target_digests[$index]}" ]] || fail idempotence target-drift
            case "${target_backup_states[$index]}" in
                absent)
                    if [[ -e "$backup" || -L "$backup" ]]; then
                        [[ -f "$backup" && ! -L "$backup" ]] || fail backup missing-or-invalid
                        fail idempotence backup-drift
                    fi
                    after_backup_states+=("absent")
                    after_backup_digests+=("")
                    ;;
                regular)
                    [[ -f "$backup" && ! -L "$backup" ]] || fail backup missing-or-invalid
                    [[ "$(sha256 "$backup")" == "${target_backup_digests[$index]}" ]] || fail idempotence backup-drift
                    after_backup_states+=("regular")
                    after_backup_digests+=("$(sha256 "$backup")")
                    ;;
                *)
                    fail internal unclassified
                    ;;
            esac
            ;;
        unpatched)
            [[ "$target_digest" != "${target_digests[$index]}" ]] || fail verify target-untransformed
            [[ -f "$backup" && ! -L "$backup" ]] || fail backup missing-or-invalid
            [[ "$(sha256 "$backup")" == "${target_digests[$index]}" ]] || fail backup digest-mismatch
            after_backup_states+=("regular")
            after_backup_digests+=("$(sha256 "$backup")")
            ;;
        *)
            fail internal unclassified
            ;;
    esac
    after_target_digests+=("$target_digest")
done

for index in "${!non_targets[@]}"; do
    [[ "$(sha256 "${non_targets[$index]}")" == "${non_target_digests[$index]}" ]] || fail verify non-target-drift
done

if ! bash "$PATCH_SCRIPT" "$OPENCLAW_ROOT" >"$VERIFY_DIR/patch-output" 2>&1; then
    fail patch command-failed
fi

for index in "${!targets[@]}"; do
    target="${targets[$index]}"
    backup="${target}.pre-offload-patch.bak"
    has_injected_messages "$target" || fail verify target-untransformed
    [[ "$(sha256 "$target")" == "${after_target_digests[$index]}" ]] || fail idempotence target-drift
    case "${after_backup_states[$index]}" in
        absent)
            [[ ! -e "$backup" && ! -L "$backup" ]] || fail idempotence backup-drift
            ;;
        regular)
            [[ -f "$backup" && ! -L "$backup" ]] || fail idempotence backup-drift
            [[ "$(sha256 "$backup")" == "${after_backup_digests[$index]}" ]] || fail idempotence backup-drift
            ;;
        *)
            fail internal unclassified
            ;;
    esac
done

for index in "${!non_targets[@]}"; do
    [[ "$(sha256 "${non_targets[$index]}")" == "${non_target_digests[$index]}" ]] || fail idempotence target-drift
done
