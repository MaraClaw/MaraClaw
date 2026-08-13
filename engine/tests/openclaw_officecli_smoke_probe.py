from typing import Final

TENCENT_MARKER: Final = ".bootstrap-tencentdb-version"
GOG_MARKER: Final = "gogcli/.gog-version-checked"
CHILD_MARKER: Final = ".officecli-smoke-child"

PROBE: Final = f"""set -eu
test "$(id -un)" = node
test "$(id -u)" -ne 0
test -w "$OPENCLAW_STATE_DIR"
test "$(command -v officecli)" = "$OPENCLAW_STATE_DIR/.officecli/current/officecli"
test "$OFFICECLI_SKIP_UPDATE" = 1
test "$OFFICECLI_NO_AUTO_RESIDENT" = 1
test -f "$OPENCLAW_STATE_DIR/.officecli/current/skill/SKILL.md" && test ! -L "$OPENCLAW_STATE_DIR/.officecli/current/skill/SKILL.md"
test -f "$GOG_KEYRING_PASSWORD_FILE" && test -n "$GOG_KEYRING_PASSWORD"
test "$(node --version)" = "v26.5.0"
test "$(node -p 'process.arch')" = "arm64"
node --expose-internals -e 'require("internal/deps/acorn/acorn/dist/acorn").parse("export{{}}",{{sourceType:"module"}})'
officecli_version="$(officecli --version)"
printf '%s\n' "$officecli_version" | grep -Eq '(^|[^0-9])v?1\\.0\\.143([^0-9]|$)'
openclaw skills info officecli --json >/dev/null
document="/tmp/officecli-smoke-$$.docx"
officecli create "$document"
officecli validate "$document"
test -s "$document"
touch "$OPENCLAW_STATE_DIR/{CHILD_MARKER}"
"""
