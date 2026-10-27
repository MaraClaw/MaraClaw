from typing import Final

GOG_MARKER: Final = "gogcli/.gog-version-checked"
CHILD_MARKER: Final = ".officecli-smoke-child"
MEMORY_MARKER: Final = ".native-memory-smoke-results.json"

NATIVE_MEMORY_PROBE: Final = f"""
node -e '
const fs = require("node:fs");
const state = process.env.OPENCLAW_STATE_DIR;
const workspace = `${{state}}/workspace`;
fs.mkdirSync(workspace, {{recursive: true}});
fs.writeFileSync(`${{workspace}}/MEMORY.md`, "The maraclawftsprobe launch code is violet-orbit-482.\\n");
fs.writeFileSync(`${{state}}/openclaw.json`, JSON.stringify({{
  agents: {{defaults: {{workspace}}}},
  memory: {{search: {{enabled: true, provider: "none"}}}},
  plugins: {{slots: {{memory: "memory-core"}}}}
}}));
'
openclaw config validate
openclaw memory index --force --agent main
openclaw memory search --agent main --query maraclawftsprobe --json > "$OPENCLAW_STATE_DIR/{MEMORY_MARKER}"
node -e '
const fs = require("node:fs");
const results = JSON.parse(fs.readFileSync(`${{process.env.OPENCLAW_STATE_DIR}}/{MEMORY_MARKER}`, "utf8"));
if (!results.results.some(result => result.path === "MEMORY.md" && result.snippet.includes("violet-orbit-482"))) {{
  throw new Error("native FTS did not retrieve the workspace memory fact");
}}
'
"""

MANDATORY_PROBE: Final = """set -eu
test "$(id -un)" = node
test "$(id -u)" -ne 0
test -w "$OPENCLAW_STATE_DIR"
test -f "$GOG_KEYRING_PASSWORD_FILE" && test -n "$GOG_KEYRING_PASSWORD"
test "$(node --version)" = "v26.12.1"
test "$(node -p 'process.arch')" = "$EXPECTED_NODE_ARCH"
printf 'executed node=%s arch=%s kernel=%s\n' "$(node --version)" "$(node -p 'process.arch')" "$(uname -m)"
gog --version | grep '0.47.1'
openclaw --version | grep '2026.10.7'
"""

PROBE: Final = (
    MANDATORY_PROBE
    + f"""
test "$(command -v officecli)" = "$OPENCLAW_STATE_DIR/.officecli/current/officecli"
test "$OFFICECLI_SKIP_UPDATE" = 1
test "$OFFICECLI_NO_AUTO_RESIDENT" = 1
test -f "$OPENCLAW_STATE_DIR/.officecli/current/skill/SKILL.md" && test ! -L "$OPENCLAW_STATE_DIR/.officecli/current/skill/SKILL.md"
officecli_version="$(officecli --version)"
printf '%s\n' "$officecli_version" | grep -Eq '(^|[^0-9])v?1\\.0\\.158([^0-9]|$)'
printf 'executed %s\n' "$officecli_version"
officecli --help > /tmp/officecli-help.txt
test -s /tmp/officecli-help.txt
openclaw skills info officecli --json | node -e '
const fs = require("node:fs");
const skill = JSON.parse(fs.readFileSync(0, "utf8"));
if (skill.name !== "officecli" || skill.eligible !== true || skill.modelVisible !== true) {{
  throw new Error("OfficeCLI skill is not ready for the agent");
}}
'
for skill in word pptx excel; do
  officecli load_skill "$skill" > "/tmp/officecli-skill-$skill.md"
  test -s "/tmp/officecli-skill-$skill.md"
done
document="/tmp/officecli-smoke-$$.docx"
officecli create "$document"
officecli validate "$document"
test -s "$document"
touch "$OPENCLAW_STATE_DIR/{CHILD_MARKER}"
"""
)

INCOMPATIBLE_OFFLINE_PROBE: Final = (
    MANDATORY_PROBE
    + f"""
! command -v officecli
test -L "$OPENCLAW_STATE_DIR/.officecli/current"
touch "$OPENCLAW_STATE_DIR/{CHILD_MARKER}"
"""
)

# OfficeCLI is runtime-only and optional when neither download nor LKG exists.
EMPTY_OFFLINE_PROBE: Final = (
    MANDATORY_PROBE
    + NATIVE_MEMORY_PROBE
    + f"""
! command -v officecli
test ! -e "$OPENCLAW_STATE_DIR/.officecli/current"
test ! -L "$OPENCLAW_STATE_DIR/.officecli/current"
test ! -e "$OPENCLAW_STATE_DIR/skills/officecli"
touch "$OPENCLAW_STATE_DIR/{CHILD_MARKER}"
"""
)
