import json
import re
import shlex
from pathlib import Path
from typing import Final

REPO_ROOT: Final = Path(__file__).resolve().parents[1]
DOCKERFILE: Final = REPO_ROOT / "Dockerfile.openclaw"
Instruction = tuple[str, str]
FileInstruction = tuple[str, tuple[str, ...], str, tuple[str, ...]]
EXPECTED_OFFICECLI_ENV: Final = (
    ("OFFICECLI_NO_AUTO_RESIDENT", "1"),
    ("OFFICECLI_SKIP_UPDATE", "1"),
)
EXPECTED_ENTRYPOINT: Final = ("tini", "-s", "--", "/usr/local/bin/bootstrap-officecli.sh")
EXPECTED_CMD: Final = ("/usr/local/bin/validate-gogcli.sh", "openclaw", "gateway")
EXPECTED_CHMOD_RUN: Final = (
    "chmod 0755 /usr/local/bin/bootstrap-officecli.sh && "
    "chmod 0755 /usr/local/bin/bootstrap-memory-tencentdb.sh && "
    "chmod 0755 /usr/local/bin/validate-gogcli.sh && "
    "chmod 0755 /opt/openclaw-plugin-cache/openclaw-after-tool-call-messages.patch.sh"
)
OFFICECLI_COPY: Final[FileInstruction] = (
    "COPY",
    ("docker/openclaw/bootstrap-officecli.sh",),
    "/usr/local/bin/bootstrap-officecli.sh",
    ("--chown=node:node",),
)
REQUIRED_COPIES: Final = (
    (OFFICECLI_COPY, "OfficeCLI helper"),
    (
        (
            "COPY",
            ("docker/openclaw/bootstrap-memory-tencentdb.sh",),
            "/usr/local/bin/bootstrap-memory-tencentdb.sh",
            ("--chown=node:node",),
        ),
        "TencentDB bootstrap",
    ),
    (
        (
            "COPY",
            ("docker/openclaw/validate-gogcli.sh",),
            "/usr/local/bin/validate-gogcli.sh",
            ("--chown=node:node",),
        ),
        "Gogcli validator",
    ),
)
REGRESSION_CASES: Final = (
    ("OFFICECLI_SKIP_UPDATE=1", "OFFICECLI_SKIP_UPDATE=0", "OfficeCLI runtime flags"),
    (" OFFICECLI_NO_AUTO_RESIDENT=1", "", "OfficeCLI runtime flags"),
    ("OFFICECLI_SKIP_UPDATE=1", "OFFICECLI_SKIP_UPDATE", "OfficeCLI runtime flags"),
    ("USER node\nENTRYPOINT", "USER root\nENTRYPOINT", "final USER"),
    ("ENTRYPOINT", "ENTRYPOINT_BROKEN", "ENTRYPOINT"),
    ('"tini"', '"not-tini"', "ENTRYPOINT"),
    ('bootstrap-officecli.sh"]', 'bootstrap-memory-tencentdb.sh"]', "ENTRYPOINT"),
    ("CMD", "CMD_BROKEN", "CMD"),
    ('"gateway"]', '"agent"]', "CMD"),
    ("docker/openclaw/bootstrap-officecli.sh", "docker/openclaw/bootstrap-other.sh", "OfficeCLI helper COPY"),
    ("RUN chmod 0755 /usr/local/bin/bootstrap-officecli.sh", "RUN true", "OfficeCLI helper chmod"),
    (
        "COPY --chown=node:node docker/openclaw/bootstrap-memory-tencentdb.sh /usr/local/bin/bootstrap-memory-tencentdb.sh\nCOPY --chown=node:node docker/openclaw/validate-gogcli.sh /usr/local/bin/validate-gogcli.sh",
        "# COPY --chown=node:node docker/openclaw/bootstrap-memory-tencentdb.sh /usr/local/bin/bootstrap-memory-tencentdb.sh\n# COPY --chown=node:node docker/openclaw/validate-gogcli.sh /usr/local/bin/validate-gogcli.sh",
        "TencentDB bootstrap COPY",
    ),
    (
        "RUN chmod 0755 /usr/local/bin/bootstrap-officecli.sh",
        "RUN true || chmod 0755 /usr/local/bin/bootstrap-officecli.sh",
        "OfficeCLI helper chmod",
    ),
    (
        "RUN chmod 0755 /usr/local/bin/bootstrap-officecli.sh",
        "RUN false && chmod 0755 /usr/local/bin/bootstrap-officecli.sh || true",
        "OfficeCLI helper chmod",
    ),
    (
        "&& chmod 0755 /usr/local/bin/bootstrap-memory-tencentdb.sh",
        "'&&' chmod 0755 /usr/local/bin/bootstrap-memory-tencentdb.sh",
        "OfficeCLI helper chmod",
    ),
    (
        "USER node\nENTRYPOINT",
        "COPY vendor/officecli /usr/local/bin/officecli\nUSER node\nENTRYPOINT",
        "OfficeCLI COPY/ADD",
    ),
    (
        'CMD ["/usr/local/bin/validate-gogcli.sh", "openclaw", "gateway"]',
        'CMD ["/usr/local/bin/validate-gogcli.sh", "openclaw", "gateway"]\nENV OFFICECLI_AUTO_INSTALL=1 ' + "\\",
        "dangling Dockerfile continuation",
    ),
    (
        'CMD ["/usr/local/bin/validate-gogcli.sh", "openclaw", "gateway"]',
        'CMD ["/usr/local/bin/validate-gogcli.sh", "openclaw", "gateway"]\nRUN echo "unterminated',
        "malformed shell tokenization",
    ),
    ("@tencentdb-agent-memory/memory-tencentdb", "@other/memory", "TencentDB archive"),
    ("docker/openclaw/validate-gogcli.sh", "docker/openclaw/validate-other.sh", "Gogcli validator COPY"),
    ("ARG GOGCLI_SHA256=", "ARG GOGCLI_SHA256_BROKEN=", "Gogcli checksum"),
    ("ARG TENCENTDB_PLUGIN_VERSION=1.0.1", "ARG OFFICECLI_VERSION=1.2.3", "OfficeCLI ARG pins"),
    (
        "RUN chmod 0755",
        "RUN curl https://api.github.com/repos/iOfficeAI/OfficeCLI\nRUN chmod 0755",
        "OfficeCLI build-time",
    ),
    (
        "RUN chmod 0755",
        "RUN curl https://github.com/iOfficeAI/OfficeCLI/releases/latest/download/officecli\nRUN chmod 0755",
        "OfficeCLI build-time",
    ),
    ("RUN chmod 0755", "RUN wget https://mirror.example/officecli\nRUN chmod 0755", "OfficeCLI build-time"),
    (
        "RUN chmod 0755",
        "RUN curl https://raw.githubusercontent.com/iOfficeAI/OfficeCLI/main/SKILL.md\nRUN chmod 0755",
        "OfficeCLI build-time",
    ),
    ("RUN chmod 0755", "RUN curl https://officecli.example/install.sh\nRUN chmod 0755", "OfficeCLI build-time"),
    ("RUN chmod 0755", "RUN officecli install\nRUN chmod 0755", "OfficeCLI build-time"),
    ("USER node", "ENV OFFICECLI_AUTO_INSTALL=1\nUSER node", "OfficeCLI runtime flags"),
)


def _instructions(source: str) -> tuple[tuple[Instruction, ...], tuple[str, ...]]:
    instructions: list[Instruction] = []
    errors: list[str] = []
    pending = ""
    for raw_line in source.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        pending = f"{pending} {line}".strip()
        if line.endswith("\\"):
            pending = pending[:-1].rstrip()
            continue
        keyword, separator, body = pending.partition(" ")
        if separator:
            body = body.strip()
            try:
                shlex.split(body)
            except ValueError:
                errors.append("malformed shell tokenization")
            else:
                instructions.append((keyword.upper(), body))
        pending = ""
    if pending:
        errors.append("malformed dangling Dockerfile continuation")
    return tuple(instructions), tuple(errors)


def _arguments(body: str) -> tuple[str, ...]:
    return tuple(shlex.split(body))


def _json_array(body: str) -> tuple[str, ...]:
    try:
        values = json.loads(body)
    except json.JSONDecodeError:
        return ()
    if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
        return ()
    return tuple(values)


def _instruction_values(instructions: tuple[Instruction, ...], keyword: str) -> tuple[str, ...]:
    return tuple(body for name, body in instructions if name == keyword)


def _file_instructions(instructions: tuple[Instruction, ...]) -> tuple[FileInstruction, ...]:
    parsed: list[FileInstruction] = []
    for kind in ("COPY", "ADD"):
        for body in _instruction_values(instructions, kind):
            arguments = _arguments(body)
            options = tuple(argument for argument in arguments if argument.startswith("--"))
            paths = tuple(argument for argument in arguments if not argument.startswith("--"))
            if len(paths) >= 2:
                parsed.append((kind, paths[:-1], paths[-1], options))
    return tuple(parsed)


def _contract_errors(source: str) -> tuple[str, ...]:
    instructions, parse_errors = _instructions(source)
    errors = list(parse_errors)
    environment = tuple(
        (name, value)
        for body in _instruction_values(instructions, "ENV")
        for argument in _arguments(body)
        for name, separator, value in (argument.partition("="),)
        if separator
    )
    officecli_environment = tuple(sorted(pair for pair in environment if pair[0].startswith("OFFICECLI_")))
    if officecli_environment != EXPECTED_OFFICECLI_ENV:
        errors.append("OfficeCLI runtime flags must be exactly the two documented values")
    if ("OPENCLAW_MEMORY_TENCENTDB_ARCHIVE", "/opt/openclaw-plugin-cache/memory-tencentdb.tgz") not in environment:
        errors.append("missing TencentDB archive environment")

    users = _instruction_values(instructions, "USER")
    if not users or users[-1] != "node":
        errors.append("final USER must remain node")

    entrypoints = _instruction_values(instructions, "ENTRYPOINT")
    if not entrypoints or _json_array(entrypoints[-1]) != EXPECTED_ENTRYPOINT:
        errors.append("ENTRYPOINT must be tini wrapping bootstrap-officecli.sh")

    commands = _instruction_values(instructions, "CMD")
    if not commands or _json_array(commands[-1]) != EXPECTED_CMD:
        errors.append("CMD must remain the Gogcli validator and gateway")

    file_instructions = _file_instructions(instructions)
    for required_copy, label in REQUIRED_COPIES:
        if file_instructions.count(required_copy) != 1:
            errors.append(f"required {label} COPY semantics missing")

    officecli_file_instructions = tuple(
        instruction
        for instruction in file_instructions
        if any("officecli" in path.lower() for path in (*instruction[1], instruction[2]))
    )
    if officecli_file_instructions != (OFFICECLI_COPY,):
        errors.append("OfficeCLI COPY/ADD must contain only the required helper copy")

    if EXPECTED_CHMOD_RUN not in tuple(" ".join(body.split()) for body in _instruction_values(instructions, "RUN")):
        errors.append("OfficeCLI helper chmod is required")

    for label, fragment in (
        ("TencentDB archive", "@tencentdb-agent-memory/memory-tencentdb@${TENCENTDB_PLUGIN_VERSION}"),
        ("Gogcli asset", "ARG GOGCLI_VERSION="),
        ("Gogcli checksum", "ARG GOGCLI_SHA256="),
        ("Gogcli binary", "/usr/local/bin/gog"),
    ):
        if fragment not in source:
            errors.append(f"missing {label}")

    errors.extend(
        "OfficeCLI ARG pins are forbidden"
        for body in _instruction_values(instructions, "ARG")
        if body.upper().startswith("OFFICECLI_")
    )

    allowed_officecli_environment = {name for name, _value in EXPECTED_OFFICECLI_ENV}
    unexpected_environment = tuple(
        name for name, _value in officecli_environment if name not in allowed_officecli_environment
    )
    if unexpected_environment:
        errors.append("undocumented OfficeCLI auto-install controls are forbidden")

    blocked_sources = ("api.github.com", "latest/download", "latest-download", "mirror", "/main/", "install.sh")
    for name, body in instructions:
        lower_body = body.lower()
        if "officecli" not in lower_body or name not in {"RUN", "ADD"}:
            continue
        if re.search(r"\b(?:curl|wget|download|install)\b", lower_body):
            errors.append("OfficeCLI build-time network or install is forbidden")
        if any(pattern in lower_body for pattern in blocked_sources):
            errors.append("OfficeCLI build-time source is forbidden")
    return tuple(errors)


def test_dockerfile_wires_runtime_officecli_bootstrap_without_changing_runtime_chain() -> None:
    # Given
    source = DOCKERFILE.read_text(encoding="utf-8")

    # When
    errors = _contract_errors(source)

    # Then
    assert errors == ()


def test_dockerfile_contract_rejects_runtime_chain_and_build_time_regressions() -> None:
    # Given
    source = DOCKERFILE.read_text(encoding="utf-8")
    for needle, replacement, expected_error in REGRESSION_CASES:
        candidate = source.replace(needle, replacement, 1)
        assert candidate != source

        # When
        errors = _contract_errors(candidate)

        # Then
        assert any(expected_error in error for error in errors)


def test_contract_permits_existing_gogcli_download_without_officecli_build_time_install() -> None:
    # Given
    source = DOCKERFILE.read_text(encoding="utf-8")

    # When
    errors = _contract_errors(source)

    # Then
    assert errors == ()
