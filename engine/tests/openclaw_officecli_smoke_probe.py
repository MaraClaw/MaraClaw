from typing import Final

TENCENT_MARKER: Final = ".bootstrap-tencentdb-version"
GOG_MARKER: Final = "gogcli/.gog-version-checked"
CHILD_MARKER: Final = ".officecli-smoke-child"

MANDATORY_PROBE: Final = """set -eu
test "$(id -un)" = node
test "$(id -u)" -ne 0
test -w "$OPENCLAW_STATE_DIR"
test -f "$GOG_KEYRING_PASSWORD_FILE" && test -n "$GOG_KEYRING_PASSWORD"
test "$(node --version)" = "v26.11.1"
test "$(node -p 'process.arch')" = "arm64"
node --expose-internals -e 'require("internal/deps/acorn/acorn/dist/acorn").parse("export{}",{sourceType:"module"})'
test "$(cat "$OPENCLAW_STATE_DIR/.bootstrap-tencentdb-version")" = 1.0.1
node -e 'require(process.env.OPENCLAW_STATE_DIR + "/extensions/memory-tencentdb/node_modules/sqlite-vec")'
gog --version
"""

PROBE: Final = (
    MANDATORY_PROBE
    + f"""
test "$(command -v officecli)" = "$OPENCLAW_STATE_DIR/.officecli/current/officecli"
test "$OFFICECLI_SKIP_UPDATE" = 1
test "$OFFICECLI_NO_AUTO_RESIDENT" = 1
test -f "$OPENCLAW_STATE_DIR/.officecli/current/skill/SKILL.md" && test ! -L "$OPENCLAW_STATE_DIR/.officecli/current/skill/SKILL.md"
officecli_version="$(officecli --version)"
printf '%s\n' "$officecli_version" | grep -Eq '(^|[^0-9])v?1\\.0\\.144([^0-9]|$)'
openclaw skills info officecli --json >/dev/null
document="/tmp/officecli-smoke-$$.docx"
officecli create "$document"
officecli validate "$document"
test -s "$document"
touch "$OPENCLAW_STATE_DIR/{CHILD_MARKER}"
"""
)

# OfficeCLI is runtime-only and optional when neither download nor LKG exists.
EMPTY_OFFLINE_PROBE: Final = (
    MANDATORY_PROBE
    + f"""
! command -v officecli
test ! -e "$OPENCLAW_STATE_DIR/.officecli/current"
test ! -L "$OPENCLAW_STATE_DIR/.officecli/current"
test ! -e "$OPENCLAW_STATE_DIR/skills/officecli"
touch "$OPENCLAW_STATE_DIR/{CHILD_MARKER}"
"""
)
