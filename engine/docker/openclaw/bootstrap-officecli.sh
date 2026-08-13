#!/usr/bin/env sh
set -eu

OFFICECLI_TAG="v1.0.143"
OFFICECLI_ARM64_SHA256="c50298e4698fcd1b15fe1a0f096405ad260b5c84d4440882582d0bba1e57bd49"
readonly OFFICECLI_TAG OFFICECLI_ARM64_SHA256

fail() {
    printf '%s\n' "[officecli-bootstrap] $1" >&2
    exit 1
}

is_regular_file() {
    [ -f "$1" ] && [ ! -L "$1" ]
}

is_valid_tag() {
    printf '%s\n' "$1" | grep -Eq '^v[0-9]+\.[0-9]+\.[0-9]+$'
}

is_sha256() {
    printf '%s\n' "$1" | grep -Eq '^[0-9a-f]{64}$'
}

hash_file() {
    sha256sum "$1" | awk '{ print $1 }'
}

parse_metadata() {
    metadata_file="$1"
    METADATA_TAG=""
    METADATA_BINARY_DIGEST=""
    METADATA_SKILL_DIGEST=""
    metadata_count=0

    is_regular_file "$metadata_file" || return 1
    [ ! -x "$metadata_file" ] || return 1

    while IFS= read -r metadata_line || [ -n "$metadata_line" ]; do
        metadata_count=$((metadata_count + 1))
        case "$metadata_line" in
            tag=*)
                [ -z "$METADATA_TAG" ] || return 1
                METADATA_TAG="${metadata_line#tag=}"
                ;;
            binary_sha256=*)
                [ -z "$METADATA_BINARY_DIGEST" ] || return 1
                METADATA_BINARY_DIGEST="${metadata_line#binary_sha256=}"
                ;;
            skill_sha256=*)
                [ -z "$METADATA_SKILL_DIGEST" ] || return 1
                METADATA_SKILL_DIGEST="${metadata_line#skill_sha256=}"
                ;;
            *)
                return 1
                ;;
        esac
    done < "$metadata_file"

    [ "$metadata_count" -eq 3 ] || return 1
    is_valid_tag "$METADATA_TAG" || return 1
    is_sha256 "$METADATA_BINARY_DIGEST" || return 1
    is_sha256 "$METADATA_SKILL_DIGEST" || return 1
}

validate_release() {
    release_dir="$1"
    expected_tag="$2"
    binary_file="$release_dir/officecli"
    skill_dir="$release_dir/skill"
    skill_file="$skill_dir/SKILL.md"
    metadata_file="$release_dir/metadata"

    [ -d "$release_dir" ] && [ ! -L "$release_dir" ] || return 1
    [ -d "$skill_dir" ] && [ ! -L "$skill_dir" ] || return 1
    is_regular_file "$binary_file" && [ -x "$binary_file" ] || return 1
    is_regular_file "$skill_file" && [ -s "$skill_file" ] || return 1
    parse_metadata "$metadata_file" || return 1
    [ "$METADATA_TAG" = "$expected_tag" ] || return 1
    [ "$(hash_file "$binary_file")" = "$METADATA_BINARY_DIGEST" ] || return 1
    [ "$(hash_file "$skill_file")" = "$METADATA_SKILL_DIGEST" ]
}

validate_skill_link() {
    [ -L "$SKILL_LINK" ] || return 1
    [ "$(readlink "$SKILL_LINK")" = "../.officecli/current/skill" ]
}

validate_current_release() {
    [ -L "$CURRENT_LINK" ] || return 1
    current_target="$(readlink "$CURRENT_LINK")" || return 1
    case "$current_target" in
        releases/*)
            current_tag="${current_target#releases/}"
            ;;
        *)
            return 1
            ;;
    esac
    [ "$current_target" = "releases/$current_tag" ] || return 1
    is_valid_tag "$current_tag" || return 1
    validate_release "$RELEASES_DIR/$current_tag" "$current_tag" || return 1
    validate_skill_link
}

cleanup_staging() {
    if [ -n "$STAGE_DIR" ]; then
        case "$STAGE_DIR" in
            "$OFFICECLI_ROOT"/.staging.*)
                rm -rf -- "$STAGE_DIR" || :
                ;;
        esac
        STAGE_DIR=""
    fi
}

cleanup_temporary_link() {
    if [ -n "$TEMPORARY_CURRENT_LINK" ]; then
        case "$TEMPORARY_CURRENT_LINK" in
            "$OFFICECLI_ROOT"/.current.*)
                if [ -L "$TEMPORARY_CURRENT_LINK" ] || [ -f "$TEMPORARY_CURRENT_LINK" ]; then
                    rm -f -- "$TEMPORARY_CURRENT_LINK" || :
                fi
                ;;
        esac
        TEMPORARY_CURRENT_LINK=""
    fi
}

curl_safe() {
    curl --disable --config /dev/null --fail --silent --show-error --location --proto '=https' --proto-redir '=https' \
        --connect-timeout 15 --max-time 120 --retry 2 --retry-delay 1 --retry-max-time 60 --noproxy '*' "$@"
}

parse_manifest() {
    MANIFEST_DIGEST="$(awk '
        BEGIN { target = "officecli-linux-arm64" }
        {
            filename = $2
            if (NF == 2 && (filename == target || filename == "*" target)) {
                candidates++
                if (length($1) != 64 || $1 ~ /[^0-9A-Fa-f]/) malformed = 1
                else digest = tolower($1)
            } else if (NF >= 2 && filename ~ target) {
                near_name = 1
            }
        }
        END {
            if (candidates != 1 || malformed || near_name) exit 1
            print digest
        }
    ' "$STAGE_DIR/SHA256SUMS")" || return 1
    is_sha256 "$MANIFEST_DIGEST"
}

stage_release() {
    TAG="$OFFICECLI_TAG"
    STAGE_DIR="$(mktemp -d "$OFFICECLI_ROOT/.staging.XXXXXX")" || return 1
    mkdir "$STAGE_DIR/skill" || return 1
    DOWNLOAD_ROOT="https://github.com/iOfficeAI/OfficeCLI/releases/download/$OFFICECLI_TAG"
    SKILL_URL="https://raw.githubusercontent.com/iOfficeAI/OfficeCLI/$OFFICECLI_TAG/SKILL.md"
    curl_safe -o "$STAGE_DIR/officecli" "$DOWNLOAD_ROOT/officecli-linux-arm64" || return 1
    curl_safe -o "$STAGE_DIR/SHA256SUMS" "$DOWNLOAD_ROOT/SHA256SUMS" || return 1
    curl_safe -o "$STAGE_DIR/skill/SKILL.md" "$SKILL_URL" || return 1
    is_regular_file "$STAGE_DIR/SHA256SUMS" || return 1
    parse_manifest || return 1
    [ "$MANIFEST_DIGEST" = "$OFFICECLI_ARM64_SHA256" ] || return 1
    printf '%s  %s\n' "$OFFICECLI_ARM64_SHA256" "$STAGE_DIR/officecli" > "$STAGE_DIR/checksum" || return 1
    sha256sum -c "$STAGE_DIR/checksum" >/dev/null 2>&1 || return 1
    rm -f -- "$STAGE_DIR/checksum" || return 1
    chmod 0700 "$STAGE_DIR/officecli" || return 1
    chmod 0600 "$STAGE_DIR/skill/SKILL.md" || return 1
    SKILL_DIGEST="$(hash_file "$STAGE_DIR/skill/SKILL.md")" || return 1
    {
        printf 'tag=%s\n' "$TAG"
        printf 'binary_sha256=%s\n' "$OFFICECLI_ARM64_SHA256"
        printf 'skill_sha256=%s\n' "$SKILL_DIGEST"
    } > "$STAGE_DIR/metadata" || return 1
    chmod 0600 "$STAGE_DIR/metadata" || return 1
    validate_release "$STAGE_DIR" "$TAG" || return 1
    rm -f -- "$STAGE_DIR/SHA256SUMS" || return 1
}

publish_release() {
    RELEASE_DIR="$RELEASES_DIR/$TAG"
    if [ -e "$RELEASE_DIR" ] || [ -L "$RELEASE_DIR" ]; then
        validate_release "$RELEASE_DIR" "$TAG" || return 1
        cmp -s "$STAGE_DIR/officecli" "$RELEASE_DIR/officecli" || return 1
        cmp -s "$STAGE_DIR/skill/SKILL.md" "$RELEASE_DIR/skill/SKILL.md" || return 1
        cmp -s "$STAGE_DIR/metadata" "$RELEASE_DIR/metadata" || return 1
        cleanup_staging
        return 0
    fi
    mv -T -- "$STAGE_DIR" "$RELEASE_DIR" || return 1
    STAGE_DIR=""
}

prepare_skill_link() {
    if [ -e "$SKILL_LINK" ] || [ -L "$SKILL_LINK" ]; then
        validate_skill_link
        return
    fi
    ln -s "../.officecli/current/skill" "$SKILL_LINK"
}

activate_release() {
    if [ -e "$CURRENT_LINK" ] && [ ! -L "$CURRENT_LINK" ]; then
        return 1
    fi
    TEMPORARY_CURRENT_LINK="$(mktemp "$OFFICECLI_ROOT/.current.XXXXXX")" || return 1
    rm -f -- "$TEMPORARY_CURRENT_LINK" || return 1
    ln -s "releases/$TAG" "$TEMPORARY_CURRENT_LINK" || return 1
    mv -Tf -- "$TEMPORARY_CURRENT_LINK" "$CURRENT_LINK" || return 1
    TEMPORARY_CURRENT_LINK=""
}

refresh_and_activate() {
    stage_release || return 1
    publish_release || return 1
    prepare_skill_link || return 1
    activate_release
}

STATE_DIR="${OPENCLAW_STATE_DIR:-/home/node/.openclaw}"
OFFICECLI_ROOT="$STATE_DIR/.officecli"
RELEASES_DIR="$OFFICECLI_ROOT/releases"
CURRENT_LINK="$OFFICECLI_ROOT/current"
SKILL_LINK="$STATE_DIR/skills/officecli"
SCRIPT_DIR="$(CDPATH= cd "$(dirname "$0")" && pwd)"
STAGE_DIR=""
TEMPORARY_CURRENT_LINK=""

umask 077
trap 'cleanup_staging; cleanup_temporary_link' EXIT
trap 'exit 1' HUP INT TERM

[ ! -L "$STATE_DIR" ] || fail "managed state directory must not be a symlink"
mkdir -p "$STATE_DIR" "$OFFICECLI_ROOT" "$RELEASES_DIR" "$STATE_DIR/skills" || fail "could not create managed state"
[ ! -L "$STATE_DIR" ] || fail "managed state directory must not be a symlink"
[ ! -L "$OFFICECLI_ROOT" ] || fail "managed release root must not be a symlink"
[ ! -L "$RELEASES_DIR" ] || fail "managed releases directory must not be a symlink"
[ ! -L "$STATE_DIR/skills" ] || fail "managed skills directory must not be a symlink"

if ! refresh_and_activate; then
    cleanup_staging
    cleanup_temporary_link
    validate_current_release || fail "refresh failed and no valid OfficeCLI release is active"
fi

PATH="$CURRENT_LINK:$PATH"
export PATH
exec "$SCRIPT_DIR/bootstrap-memory-tencentdb.sh" "$@"
