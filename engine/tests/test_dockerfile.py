import re
import shlex
import subprocess
from pathlib import Path, PurePosixPath
from typing import Final

import pytest

DOCKERFILE: Final = Path(__file__).resolve().parents[1] / "Dockerfile"
PRODUCTION_STAGE: Final = "FROM python:3.14.8-slim-trixie AS production"
APT_INSTALL: Final = "apt-get install -y --no-install-recommends"
APPROVED_DIRECT_PINS: Final = (
    "tini=0.19.0-3+b8",
    "curl=8.14.1-2+deb13u7",
    "bubblewrap=0.12.0-1~deb13u1",
    "chromium=156.0.8078.47-1~deb13u1",
)
REGRESSION_CASES: Final = (
    ("ARG TINI_VERSION=0.19.0-3+b8", "ARG TINI_VERSION=0.19.0-3+b7", "stale tini pin"),
    ("curl=8.14.1-2+deb13u7", "curl=8.14.1-2+deb13u5", "stale curl pin"),
    ("bubblewrap=0.12.0-1~deb13u1", "bubblewrap=0.11.0-2+deb13u1", "stale bubblewrap pin"),
    ("chromium=156.0.8078.47-1~deb13u1", "chromium=154.0.8037.92-1~deb13u1", "stale Chromium pin"),
    ("curl=8.14.1-2+deb13u7", "", "missing curl direct entry"),
    ("curl=8.14.1-2+deb13u7", "curl", "unpinned curl direct entry"),
    (
        "curl=8.14.1-2+deb13u7",
        f"{APPROVED_DIRECT_PINS[1]} {chr(92)}{chr(10)}        {APPROVED_DIRECT_PINS[1]}",
        "duplicate curl direct entry",
    ),
    (
        "chromium=156.0.8078.47-1~deb13u1",
        "chromium=156.0.8078.47-1",
        "non-security Chromium replacement",
    ),
)


def _production_direct_apt_entries(source: str) -> tuple[str, ...]:
    production_stage = source.partition(PRODUCTION_STAGE)[2].partition("\nFROM ")[0]
    tini_version = production_stage.partition("ARG TINI_VERSION=")[2].partition("\n")[0]
    apt_install = production_stage.partition(APT_INSTALL)[2].replace("${TINI_VERSION}", tini_version)
    entries: list[str] = []
    for raw_line in apt_install.splitlines()[1:]:
        entry = raw_line.strip().removesuffix("\\").strip().removesuffix("&&").strip()
        if entry.startswith("fc-cache"):
            break
        if "=" in entry:
            entries.append(entry.strip("\"'"))
    return tuple(entries)


def _production_direct_pins(source: str) -> tuple[str, ...]:
    return tuple(
        entry
        for entry in _production_direct_apt_entries(source)
        if entry.partition("=")[0] in {"tini", "curl", "bubblewrap", "chromium"}
    )


def test_production_dockerfile_includes_linux_docker_cli() -> None:
    content = DOCKERFILE.read_text(encoding="utf-8")
    assert "FROM docker:29.9.2-cli AS dockercli" in content
    assert "COPY --from=dockercli /usr/local/bin/docker /usr/local/bin/docker" in content


def test_production_dockerfile_pins_approved_direct_security_packages() -> None:
    # Given
    content = DOCKERFILE.read_text(encoding="utf-8")

    # When
    direct_pins = _production_direct_pins(content)

    # Then
    assert direct_pins == APPROVED_DIRECT_PINS


@pytest.mark.parametrize(("needle", "replacement", "regression"), REGRESSION_CASES)
def test_production_dockerfile_rejects_direct_package_pin_regressions(
    needle: str, replacement: str, regression: str
) -> None:
    # Given
    source = DOCKERFILE.read_text(encoding="utf-8")
    candidate = source.replace(needle, replacement, 1)
    assert candidate != source, regression

    # When
    direct_pins = _production_direct_pins(candidate)

    # Then
    assert direct_pins != APPROVED_DIRECT_PINS, regression


EXCLUDED_INPUTS: Final = (
    ".env",
    ".env.production",
    ".venv/bin/python",
    ".docker-data/agents/customer.txt",
    ".coverage",
    ".pytest_cache/cache",
    ".ruff_cache/cache",
    "__pycache__/app.pyc",
    "maraclaw.egg-info/PKG-INFO",
)
REQUIRED_INPUTS: Final = (
    "app/main.py",
    "VERSION",
    "agent_template/SOUL.md",
    "agent_templates/role.yaml",
    "entrypoint.sh",
    "pyproject.toml",
    "uv.lock",
    "scripts/verify_anydoc.py",
    "scripts/schema_baseline.sql",
    "tests/fixtures/anydoc/supported.docx",
    "docker/openclaw/entrypoint.sh",
)


def _dependency_run(source: str) -> str:
    deps = source.partition(PRODUCTION_STAGE)[0].replace("\\\n", " ")
    return next(
        line.removeprefix("RUN ") for line in deps.splitlines() if line.startswith("RUN ") and "pip install" in line
    )


def _assert_locked_dependencies(source: str) -> None:
    deps, _, production = source.partition(PRODUCTION_STAGE)
    copies = [shlex.split(line) for line in deps.splitlines() if line.startswith("COPY ")]
    assert ["COPY", "pyproject.toml", "uv.lock", "./"] in copies
    run = _dependency_run(source)
    commands = [
        shlex.split(command.strip())
        for command in re.findall(r"(?:pip install|uv export|uv pip install|rm -f)\s+[^&]+", run)
    ]
    bootstrap, export, install, cleanup = commands
    assert bootstrap[:2] == ["pip", "install"]
    assert {"--no-cache-dir", "uv==0.13.4"} <= set(bootstrap)
    assert export[:2] == ["uv", "export"]
    assert {"--locked", "--no-dev", "--no-emit-project"} <= set(export)
    assert export[export.index("--format") + 1] == "requirements.txt"
    requirements = export[export.index("--output-file") + 1]
    assert install[:3] == ["uv", "pip", "install"]
    assert {"--system", "--no-cache", "--require-hashes"} <= set(install)
    assert install[install.index("-r") + 1] == requirements
    assert cleanup == ["rm", "-f", requirements]
    assert not {"--extra", "--all-extras", "--only-dev", "--all-groups", "--group", "--no-hashes"} & set(export)
    assert not {"--index-url", "--trusted-host", "$@"} & set(export)
    for path in ("/usr/local/lib/python3.14/site-packages/", "/usr/local/bin/"):
        assert f"COPY --from=deps {path} {path}" in production
    assert "COPY --chown=maraclaw:maraclaw . ." in production
    assert "RUN python /app/scripts/verify_anydoc.py" in production
    # Execute only this RUN with command stubs: capture argv, never install or remove files.
    stubs = "\n".join(
        f"{name}() {{ printf '{name}'; printf '\\t%s' \"$@\"; printf '\\n'; }}" for name in ("pip", "uv", "rm")
    )
    for index, host in (
        (None, None),
        ("https://mirror.example/simple", None),
        (None, "mirror.example"),
        ("", ""),
        ("https://mirror.example/simple", ""),
        ("", "mirror.example"),
        ("https://mirror.example/simple", "mirror.example"),
    ):
        result = subprocess.run(
            ["/bin/sh"],
            input=f"{stubs}\n{run}",
            check=True,
            capture_output=True,
            text=True,
            env={
                name: value
                for name, value in (("MARACLAW_PIP_INDEX_URL", index), ("MARACLAW_PIP_TRUSTED_HOST", host))
                if value is not None
            },
        )
        observed = [line.split("\t") for line in result.stdout.splitlines()]
        assert len(observed) == 4
        expected = (["--index-url", index] if index not in (None, "") else []) + (
            ["--trusted-host", host] if host not in (None, "") else []
        )
        assert observed[0] == ["pip", "install", "--no-cache-dir", *expected, "uv==0.13.4"]
        assert observed[1] == export
        assert observed[2] == [
            "uv",
            "pip",
            "install",
            "--system",
            "--no-cache",
            "--require-hashes",
            *expected,
            "-r",
            requirements,
        ]
        assert observed[3] == cleanup


def _ignored(source: str, filename: str) -> bool:
    """Bounded relative-glob policy model, not a general Docker pattern implementation.

    Supports recursive **, ancestor directory exclusion, and ordered ! negations.
    Escapes, Docker preprocessing, and symlinks are outside this test's scope.
    """
    path = PurePosixPath(filename)
    ignored = False
    for line in source.splitlines():
        pattern = line.strip()
        if not pattern or pattern.startswith("#"):
            continue
        include = pattern.startswith("!")
        glob = pattern.removeprefix("!").strip("/")
        if any(candidate.full_match(glob) for candidate in (path, *path.parents[:-1])):
            ignored = not include
    return ignored


def _assert_context_policy(source: str) -> None:
    for prefix in ("", "nested/deeper/"):
        for filename in EXCLUDED_INPUTS:
            assert _ignored(source, prefix + filename), prefix + filename
    for filename in REQUIRED_INPUTS:
        assert not _ignored(source, filename), filename


def test_dependencies_are_locked_system_installs() -> None:
    # Given / When / Then
    _assert_locked_dependencies(DOCKERFILE.read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    ("needle", "replacement"),
    [
        ("COPY pyproject.toml uv.lock ./", "COPY pyproject.toml ./"),
        ("--locked", "--frozen"),
        ("--system", ""),
        ("--no-dev", "--only-dev"),
        ("--no-dev", "--no-dev --extra dev"),
        ("--no-dev", "--no-dev --all-extras"),
        ("uv==0.13.4", "uv"),
        ("--require-hashes", ""),
        ('--no-cache-dir "$@"', "--no-cache-dir"),
        ('--require-hashes "$@"', "--require-hashes"),
        ('--index-url "$MARACLAW_PIP_INDEX_URL"', ""),
        ('--trusted-host "$MARACLAW_PIP_TRUSTED_HOST"', ""),
        (
            "--output-file /tmp/requirements.txt",
            '--index-url "$MARACLAW_PIP_INDEX_URL" --output-file /tmp/requirements.txt',
        ),
        ("rm -f /tmp/requirements.txt", "rm -f /tmp/unrelated.txt"),
    ],
)
def test_dependencies_reject_policy_mutations(needle: str, replacement: str) -> None:
    # Given
    source = DOCKERFILE.read_text(encoding="utf-8")
    candidate = source.replace(needle, replacement, 1)
    assert candidate != source
    # When / Then
    with pytest.raises(AssertionError):
        _assert_locked_dependencies(candidate)


def test_context_excludes_local_inputs_and_preserves_runtime_inputs() -> None:
    # Given / When / Then
    _assert_context_policy(DOCKERFILE.with_name(".dockerignore").read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    "pattern",
    [
        ".env",
        ".env.*",
        ".venv",
        ".docker-data",
        ".coverage",
        ".pytest_cache",
        ".ruff_cache",
        "__pycache__",
        "*.egg-info",
    ],
)
def test_context_rejects_missing_exclusions(pattern: str) -> None:
    # Given
    source = DOCKERFILE.with_name(".dockerignore").read_text(encoding="utf-8")
    candidate = source.replace(f"**/{pattern}\n", "", 1)
    assert candidate != source
    # When / Then
    with pytest.raises(AssertionError):
        _assert_context_policy(candidate)


@pytest.mark.parametrize("pattern", [*REQUIRED_INPUTS, "tests/", "scripts/", "**/supported.docx", "!.env"])
def test_context_rejects_unsafe_policy_additions(pattern: str) -> None:
    # Given
    source = DOCKERFILE.with_name(".dockerignore").read_text(encoding="utf-8")
    # When / Then
    with pytest.raises(AssertionError):
        _assert_context_policy(f"{source}\n{pattern}\n")
