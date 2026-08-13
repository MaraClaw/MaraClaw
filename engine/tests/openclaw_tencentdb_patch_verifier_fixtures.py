import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from openclaw_tencentdb_patch_scenario_fixtures import (
    EXACT_MESSAGES_PROPERTY as EXACT_MESSAGES_PROPERTY,
    MixedPatchScenario as MixedPatchScenario,
    backup_path as backup_path,
    create_mixed_patch_scenario as create_mixed_patch_scenario,
    decoy_source as decoy_source,
    prepatched_hook_source as prepatched_hook_source,
    selected_hook_source as selected_hook_source,
    write_noop_patch_asset as write_noop_patch_asset,
    write_patch_asset as write_patch_asset,
)

REPO_ROOT: Final = Path(__file__).resolve().parents[1]
VERIFIER: Final = REPO_ROOT / "docker" / "openclaw" / "verify-tencentdb-openclaw-patch.sh"
CLASSIFIER: Final = REPO_ROOT / "docker" / "openclaw" / "classify-tencentdb-openclaw-hook.cjs"
CLASSIFIER_BIN: Final = "/usr/local/bin/classify-tencentdb-openclaw-hook.cjs"
CLASSIFIER_FLOW: Final = f'''CLASSIFIER="{CLASSIFIER_BIN}"
has_injected_messages() {{
    [[ -f "$CLASSIFIER" && ! -L "$CLASSIFIER" ]] || fail internal unclassified
    local classification
    if ! classification="$(node --expose-internals "$CLASSIFIER" "$1")"; then
        fail internal unclassified
    fi
    case "$classification" in
        prepatched) return 0 ;;
        unpatched) return 1 ;;
        *) fail internal unclassified ;;
    esac
}}
'''


@dataclass(frozen=True, slots=True)
class ProbeConfig:
    state: str = "prepatched"
    status: int = 0
    output: str | None = None
    classifier_kind: str = "regular"


@dataclass(frozen=True, slots=True)
class ProbeResult:
    result: subprocess.CompletedProcess[str]
    calls: tuple[tuple[str, ...], ...]
    target: Path
    decoy: Path
    classifier: Path
    canary: str


def require_command(command: str) -> str:
    path = shutil.which(command)
    assert path is not None
    return path


def run_verifier(root: Path, asset: Path, *, mode: str = "all") -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["PATCH_MODE"] = mode
    return subprocess.run(  # noqa: S603 - controlled repository helper and temporary fixture.
        [require_command("bash"), str(VERIFIER), str(root), str(asset)],
        cwd=REPO_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )


def run_classifier(target: Path, *, expose_internals: bool = True) -> subprocess.CompletedProcess[str]:
    assert CLASSIFIER.is_file()
    command = [require_command("node")]
    if expose_internals:
        command.append("--expose-internals")
    command.extend((str(CLASSIFIER), str(target)))
    return subprocess.run(  # noqa: S603 - controlled future classifier and temporary fixture.
        command,
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def classifier_verifier_source() -> str:
    source = VERIFIER.read_text(encoding="utf-8")
    start = source.index("has_injected_messages() {")
    end = source.index("\n}\n", start) + 3
    return f"{source[:start]}{CLASSIFIER_FLOW}{source[end:]}"


def classifier_decoy_source(kind: str) -> str:
    match kind:
        case "comment":
            decoy = "\n".join(f"# {line}" for line in CLASSIFIER_FLOW.splitlines())
        case "heredoc":
            decoy = f": <<'FLOW'\n{CLASSIFIER_FLOW}FLOW"
        case "function":
            decoy = f"never() {{\n{CLASSIFIER_FLOW}}}"
        case "assignment":
            decoy = f"unused='\n{CLASSIFIER_FLOW}'"
        case unreachable:
            raise AssertionError(f"unknown classifier decoy: {unreachable}")
    return classifier_verifier_source().replace(CLASSIFIER_FLOW, f"has_injected_messages() {{ return 0; }}\n{decoy}")


def run_classifier_probe(tmp_path: Path, source: str, config: ProbeConfig | None = None) -> ProbeResult:
    config = config or ProbeConfig()
    root = tmp_path / "probe-openclaw"
    dist = root / "dist"
    dist.mkdir(parents=True)
    target = dist / "selected-hook.js"
    target.write_text(prepatched_hook_source(), encoding="utf-8")
    decoy = dist / "decoy.js"
    decoy.write_text(decoy_source(), encoding="utf-8")
    asset = tmp_path / "noop-patch.sh"
    write_noop_patch_asset(asset)
    classifier = tmp_path / "classifier.cjs"
    if config.classifier_kind == "regular":
        classifier.write_text("", encoding="utf-8")
    elif config.classifier_kind == "symlink":
        classifier.symlink_to(target)
    verifier = tmp_path / "verify.sh"
    verifier.write_text(source.replace(CLASSIFIER_BIN, str(classifier)), encoding="utf-8")
    verifier.chmod(0o755)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    log = tmp_path / "node.jsonl"
    canary = "fake-node-canary"
    fake_node = fake_bin / "node"
    fake_node.write_text(
        f"#!{sys.executable}\nimport json, os, sys\nopen(os.environ['PROBE_LOG'], 'a').write(json.dumps(sys.argv[1:]) + '\\n')\nsys.stdout.write(os.environ['PROBE_OUTPUT'])\nsys.exit(int(os.environ['PROBE_STATUS']))\n",
        encoding="utf-8",
    )
    fake_node.chmod(0o755)
    environment = os.environ | {
        "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
        "PROBE_LOG": str(log),
        "PROBE_OUTPUT": config.output if config.output is not None else f"{config.state}\n",
        "PROBE_STATUS": str(config.status),
        "PROBE_CANARY": canary,
    }
    result = subprocess.run(  # noqa: S603 - controlled temporary verifier probe.
        [require_command("bash"), str(verifier), str(root), str(asset)],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    calls = tuple(tuple(json.loads(line)) for line in log.read_text(encoding="utf-8").splitlines()) if log.exists() else ()  # fmt: skip
    return ProbeResult(result, calls, target, decoy, classifier, canary)


def assert_node_syntax(*targets: Path) -> None:
    node = require_command("node")
    for target in targets:
        result = subprocess.run(  # noqa: S603 - controlled temporary JavaScript fixture.
            [node, "--check", str(target)], capture_output=True, text=True, check=False
        )
        assert result.returncode == 0, result.stderr


def assert_redacted_failure(result: subprocess.CompletedProcess[str], expected: str, *forbidden_values: str) -> None:
    assert result.returncode != 0
    assert result.stdout == ""
    assert result.stderr == f"[tencentdb-patch-verify] {expected}\n"
    for forbidden in forbidden_values:
        assert forbidden not in result.stderr
