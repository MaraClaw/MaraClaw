from dataclasses import dataclass
from pathlib import Path
from typing import Final

EXACT_MESSAGES_PROPERTY: Final = "messages: ctx.params.session?.messages"


@dataclass(frozen=True, slots=True)
class MixedPatchScenario:
    root: Path
    prepatched_without_backup: Path
    prepatched_with_backup: Path
    unpatched_targets: tuple[Path, Path]
    decoy: Path
    existing_backup: Path
    existing_backup_bytes: bytes


def selected_hook_source(properties: str = "durationMs") -> str:
    return f"""const after_tool_call = true;
const hookEvent = {{
  {properties}
}};
hookRunnerAfter();
"""


def prepatched_hook_source(*, messages_first: bool = True) -> str:
    properties = (
        "messages    : ctx.params.session?.messages,\n  durationMs"
        if messages_first
        else f"durationMs,\n  {EXACT_MESSAGES_PROPERTY}"
    )
    return selected_hook_source(properties)


def decoy_source() -> str:
    return """const after_tool_call = true;
const hookEvent = { durationMs: 1 };
const unrelated = true;
"""


def backup_path(target: Path) -> Path:
    return target.with_name(f"{target.name}.pre-offload-patch.bak")


def write_noop_patch_asset(path: Path) -> None:
    path.write_text("#!/usr/bin/env bash\nset -eu\nexit 0\n", encoding="utf-8")
    path.chmod(0o755)


def write_patch_asset(path: Path) -> None:
    path.write_text(
        """#!/usr/bin/env bash
set -eu
root="$1"
mode="${PATCH_MODE:-all}"
runs_path="$root/.fixture-runs"
runs=0
if [ -f "$runs_path" ]; then
    runs="$(cat "$runs_path")"
fi
runs=$((runs + 1))
printf '%s\\n' "$runs" > "$runs_path"
for target in "$root"/dist/*.js; do
    [ -f "$target" ] || continue
    if perl -0777 -ne 'exit 0 if /hookEvent\\s*=\\s*\\{[\\s\\S]{0,500}?messages\\s*:\\s*ctx\\.params\\.session\\?\\s*\\.messages[\\s\\S]{0,100}?durationMs/; exit 1' "$target"; then
        continue
    fi
    if [ "$mode" = partial ] && [[ "$target" == *target-b.js ]]; then
        continue
    fi
    if perl -0777 -ne 'exit 0 if /after_tool_call[\\s\\S]{0,800}durationMs\\s*\\n\\s*\\};\\s*\\n\\s*hookRunnerAfter/; exit 1' "$target"; then
        if [ "$mode" != missing-backup ]; then
            cp "$target" "${target}.pre-offload-patch.bak"
        fi
        perl -0777 -i -pe 's/(durationMs)\\s*\\n(\\s*\\};\\s*\\n\\s*hookRunnerAfter)/$1,\\nmessages: ctx.params.session?.messages\\n$2/' "$target"
        if [ "$mode" = wrong-backup ]; then
            printf 'wrong backup\\n' > "${target}.pre-offload-patch.bak"
        fi
    fi
done
if [ "$mode" = first-non-target-drift ] && [ "$runs" -eq 1 ]; then
    printf '// drift\\n' >> "$root/dist/arbitrary-decoy.js"
fi
if [ "$runs" -eq 2 ] && [ "$mode" = second-target-drift ]; then
    printf '// drift\\n' >> "$root/dist/target-a.js"
fi
if [ "$runs" -eq 2 ] && [ "$mode" = second-backup-drift ]; then
    printf 'drift\\n' >> "$root/dist/target-a.js.pre-offload-patch.bak"
fi
if [ "$runs" -eq 2 ] && [ "$mode" = second-non-target-drift ]; then
    printf '// drift\\n' >> "$root/dist/arbitrary-decoy.js"
fi
""",
        encoding="utf-8",
    )
    path.chmod(0o755)


def create_mixed_patch_scenario(tmp_path: Path) -> MixedPatchScenario:
    root = tmp_path / "openclaw"
    dist = root / "dist"
    dist.mkdir(parents=True)
    prepatched_without_backup = dist / "arbitrary-prepatched-no-backup.js"
    prepatched_without_backup.write_text(prepatched_hook_source(), encoding="utf-8")
    prepatched_with_backup = dist / "arbitrary-prepatched-existing-backup.js"
    prepatched_with_backup.write_text(prepatched_hook_source(), encoding="utf-8")
    existing_backup = backup_path(prepatched_with_backup)
    existing_backup_bytes = b"existing prepatch backup\n"
    existing_backup.write_bytes(existing_backup_bytes)
    unpatched_targets = (dist / "arbitrary-unpatched-a.js", dist / "arbitrary-unpatched-b.js")
    for target in unpatched_targets:
        target.write_text(selected_hook_source(), encoding="utf-8")
    decoy = dist / "arbitrary-decoy.js"
    decoy.write_text(decoy_source(), encoding="utf-8")
    return MixedPatchScenario(
        root,
        prepatched_without_backup,
        prepatched_with_backup,
        unpatched_targets,
        decoy,
        existing_backup,
        existing_backup_bytes,
    )
