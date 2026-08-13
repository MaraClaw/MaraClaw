import shlex
from pathlib import Path
from typing import Final

from openclaw_tencentdb_patch_verifier_fixtures import (
    CLASSIFIER_BIN,
    ProbeConfig,
    classifier_decoy_source,
    classifier_verifier_source,
    run_classifier_probe,
)

REPO_ROOT: Final = Path(__file__).resolve().parents[1]
DOCKERFILE: Final = REPO_ROOT / "Dockerfile.openclaw"
VERIFIER: Final = REPO_ROOT / "docker" / "openclaw" / "verify-tencentdb-openclaw-patch.sh"
BOOTSTRAP: Final = REPO_ROOT / "docker" / "openclaw" / "bootstrap-memory-tencentdb.sh"
VERIFIER_ASSET: Final = "docker/openclaw/verify-tencentdb-openclaw-patch.sh"
VERIFIER_BIN: Final = "/usr/local/bin/verify-tencentdb-openclaw-patch.sh"
CLASSIFIER_ASSET: Final = "docker/openclaw/classify-tencentdb-openclaw-hook.cjs"
PATCH_ASSET: Final = "/opt/openclaw-plugin-cache/openclaw-after-tool-call-messages.patch.sh"
VERIFIER_COPY: Final = ("--chown=root:root", VERIFIER_ASSET, VERIFIER_BIN)
CLASSIFIER_COPY: Final = ("--chown=root:root", CLASSIFIER_ASSET, CLASSIFIER_BIN)
DYNAMIC_ROOT: Final = 'OPENCLAW_ROOT="$(npm root --global)/openclaw"'
ROOT_RUN: Final = f'{DYNAMIC_ROOT} && {VERIFIER_BIN} "$OPENCLAW_ROOT" {PATCH_ASSET}'
FINAL: Final = (
    ("USER", "node"),
    ("ENTRYPOINT", '["tini", "-s", "--", "/usr/local/bin/bootstrap-officecli.sh"]'),
    ("CMD", '["/usr/local/bin/validate-gogcli.sh", "openclaw", "gateway"]'),
)


def _instructions(source: str) -> tuple[tuple[str, str], ...]:
    result: list[tuple[str, str]] = []
    pending = ""
    heredoc = ""
    for raw_line in source.splitlines():
        line = raw_line.strip()
        if heredoc:
            heredoc = "" if line == heredoc else heredoc
            continue
        if not line or line.startswith("#"):
            continue
        pending = f"{pending} {line}".strip()
        if line.endswith("\\"):
            pending = pending[:-1].rstrip()
            continue
        name, _separator, body = pending.partition(" ")
        result.append((name.upper(), body.strip()))
        marker = body.split("<<")[-1].split() if "<<" in body else ()
        heredoc = marker[0].strip("'\"") if marker else ""
        pending = ""
    return tuple(result)


def _static_contract_errors(dockerfile_source: str, bootstrap_source: str) -> tuple[str, ...]:
    instructions = _instructions(dockerfile_source)
    root: list[tuple[str, str]] = []
    collecting = False
    for instruction in instructions:
        if instruction[0] == "USER":
            collecting = instruction[1] == "root"
            if collecting:
                root = []
        elif collecting:
            root.append(instruction)
    runs = tuple(" ".join(body.split()) for name, body in root if name == "RUN")
    copies = tuple(tuple(shlex.split(body)) for name, body in root if name == "COPY")
    errors: list[str] = []
    if VERIFIER_COPY not in copies or CLASSIFIER_COPY not in copies:
        errors.append("missing root-owned co-located classifier COPY")
    if f"chmod 0755 {VERIFIER_BIN}" not in runs or ROOT_RUN not in runs:
        errors.append("missing root verification RUN semantics")
    if any("|| true" in body for body in runs):
        errors.append("root patch invariant must not suppress failures")
    if any(name in body for instruction, body in instructions if instruction == "RUN" for name in ("hook-helpers-", "selection-")):  # fmt: skip
        errors.append("root patch invariant must not hard-code dist bundle names")
    assets = ("PATCH_SCRIPT", PATCH_ASSET, VERIFIER_BIN, CLASSIFIER_ASSET, CLASSIFIER_BIN)
    if any(asset in line for line in bootstrap_source.splitlines() if line.strip() and not line.lstrip().startswith("#") for asset in assets):  # fmt: skip
        errors.append("runtime bootstrap must not invoke the classifier or patch")
    if instructions[-3:] != FINAL:
        errors.append("final runtime must remain node with the existing entrypoint and command")
    return tuple(errors)


def _node_error(probe) -> bool:
    expected = ("--expose-internals", str(probe.classifier), str(probe.target))
    forbidden = (str(probe.target), probe.target.name, probe.canary, "selected-hook.js", "noop-patch")
    return probe.result.returncode != 0 or probe.result.stdout or probe.result.stderr or not probe.calls or any(call != expected for call in probe.calls) or any(value in probe.result.stderr for value in forbidden)  # fmt: skip


def _failure_error(probe, calls: bool) -> bool:
    expected = ("--expose-internals", str(probe.classifier), str(probe.target))
    forbidden = (str(probe.target), probe.target.name, probe.canary, "selected-hook.js", "noop-patch")
    return probe.result.returncode != 1 or probe.result.stdout or probe.result.stderr != "[tencentdb-patch-verify] stage=internal code=unclassified\n" or bool(probe.calls) != calls or (calls and any(call != expected for call in probe.calls)) or any(value in probe.result.stderr for value in forbidden)  # fmt: skip


def _verifier_contract_errors(tmp_path: Path, source: str) -> tuple[str, ...]:
    normal = run_classifier_probe(tmp_path / "normal", source)
    contradict = run_classifier_probe(tmp_path / "contradict", source, ProbeConfig(state="unpatched"))
    failures = (
        (source.replace(CLASSIFIER_BIN, "missing.cjs"), ProbeConfig(), False),
        (source, ProbeConfig(classifier_kind="symlink"), False),
        (source, ProbeConfig(status=1), True),
        (source, ProbeConfig(output="bad\n"), True),
    )
    expected = "[tencentdb-patch-verify] stage=verify code=target-untransformed\n"
    errors: list[str] = []
    if _node_error(normal) or any(_failure_error(run_classifier_probe(tmp_path / f"failure-{index}", candidate, config), calls) for index, (candidate, config, calls) in enumerate(failures)):  # fmt: skip
        errors.append("verifier must use the fail-closed Node classifier contract")
    argv = ("--expose-internals", str(contradict.classifier), str(contradict.target))
    if contradict.result.stderr != expected or contradict.result.stdout or not contradict.calls or any(call != argv for call in contradict.calls):  # fmt: skip
        errors.append("verifier must not retain the regex semantic fallback")
    return tuple(errors)


def test_tencentdb_patch_runs_once_at_root_build_time_with_semantic_verification(tmp_path: Path) -> None:
    errors = _static_contract_errors(DOCKERFILE.read_text(encoding="utf-8"), BOOTSTRAP.read_text(encoding="utf-8")) + _verifier_contract_errors(tmp_path, VERIFIER.read_text(encoding="utf-8"))  # fmt: skip
    assert errors == ()


def test_tencentdb_patch_build_contract_rejects_weakened_invariants(tmp_path: Path) -> None:
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")
    bootstrap = BOOTSTRAP.read_text(encoding="utf-8")
    root_run = f'RUN {DYNAMIC_ROOT} \\\n    && {VERIFIER_BIN} \\\n        "$OPENCLAW_ROOT" \\\n        {PATCH_ASSET}'
    compliant = dockerfile
    source = classifier_verifier_source()
    assert _static_contract_errors(compliant, bootstrap) == ()
    assert _verifier_contract_errors(tmp_path / "valid", source) == ()
    assert "verifier must use the fail-closed Node classifier contract" in _verifier_contract_errors(tmp_path / "wrong-argv", source.replace("--expose-internals ", "", 1))  # fmt: skip
    assert "verifier must use the fail-closed Node classifier contract" in _verifier_contract_errors(tmp_path / "wrong-fail", source.replace("fail internal unclassified", "fail candidate no-target"))  # fmt: skip
    for kind in ("comment", "heredoc", "function", "assignment"):
        assert _verifier_contract_errors(tmp_path / kind, classifier_decoy_source(kind)) == (
            "verifier must use the fail-closed Node classifier contract",
            "verifier must not retain the regex semantic fallback",
        )
    bad_owner = compliant.replace(f"COPY --chown=root:root {CLASSIFIER_ASSET}", f"COPY --chown=node:node {CLASSIFIER_ASSET}", 1)  # fmt: skip
    bad_destination = compliant.replace(CLASSIFIER_BIN, "/usr/local/sbin/classify-tencentdb-openclaw-hook.cjs", 1)  # fmt: skip
    mutations = (
        (bad_owner, "classifier COPY"),
        (bad_destination, "classifier COPY"),
        (compliant.replace(root_run, f'LABEL value="{ROOT_RUN}"', 1), "root verification RUN semantics"),
        (compliant.replace(root_run, f"RUN <<EOF\n{ROOT_RUN}\nEOF", 1), "root verification RUN semantics"),
        (compliant.replace(DYNAMIC_ROOT, 'OPENCLAW_ROOT="/static"', 1), "root verification RUN semantics"),
        (compliant.replace(f"&& {VERIFIER_BIN}", f"&& {VERIFIER_BIN} || true", 1), "must not suppress failures"),
        (
            compliant.replace("\nUSER node\n", "\nRUN hook-helpers-generated.js\nUSER node\n", 2),
            "hard-code dist bundle names",
        ),
        (compliant.replace('"gateway"]', '"agent"]', 1), "final runtime"),
    )
    for candidate, expected in mutations:
        assert expected in "\n".join(_static_contract_errors(candidate, bootstrap))
