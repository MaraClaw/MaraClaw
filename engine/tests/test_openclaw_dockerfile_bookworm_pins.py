import subprocess
from pathlib import Path
from typing import Final

import pytest

REPO_ROOT: Final = Path(__file__).resolve().parents[1]
DOCKERFILE: Final = REPO_ROOT / "Dockerfile.openclaw"
REQUIRED_ARGUMENTS: Final = (
    ("BASH_VERSION", "5.2.15-2+b13"),
    ("CURL_VERSION", "7.88.1-10+deb12u15"),
    ("LIBCURL4_VERSION", "7.88.1-10+deb12u15"),
    ("LIBICU72_VERSION", "72.1-3+deb12u1"),
)
REQUIRED_INSTALLS: Final = (
    "bash=${BASH_VERSION}",
    "curl=${CURL_VERSION}",
    "libcurl4=${LIBCURL4_VERSION}",
    "libicu72=${LIBICU72_VERSION}",
)
VALID_BOOKWORM_PIN_BLOCK: Final = """\
ARG BASH_VERSION=5.2.15-2+b13
ARG CURL_VERSION=7.88.1-10+deb12u15
ARG LIBCURL4_VERSION=7.88.1-10+deb12u15
RUN apt-get update \\
    && apt-get install -y --no-install-recommends \\
    "bash=${BASH_VERSION}" \\
    "curl=${CURL_VERSION}" \\
    "libcurl4=${LIBCURL4_VERSION}" \\
    && rm -rf /var/lib/apt/lists/*
"""


def _argument_values(source: str) -> dict[str, str]:
    arguments: dict[str, str] = {}
    for raw_line in source.splitlines():
        line = raw_line.strip()
        if not line.startswith("ARG "):
            continue
        name, separator, value = line.removeprefix("ARG ").partition("=")
        if separator:
            arguments[name] = value
    return arguments


def _apt_install_packages(source: str) -> tuple[str, ...]:
    install_start = source.find("apt-get install -y --no-install-recommends")
    cleanup_start = source.find("&& rm -rf /var/lib/apt/lists/*", install_start)
    if install_start < 0 or cleanup_start < 0:
        return ()
    return tuple(
        line.strip().removesuffix("\\").strip().strip('"')
        for line in source[install_start:cleanup_start].splitlines()
        if line.lstrip().startswith('"')
    )


def _bookworm_pin_errors(source: str) -> tuple[str, ...]:
    arguments = _argument_values(source)
    packages = _apt_install_packages(source)
    errors = [f"{name} must equal {value}" for name, value in REQUIRED_ARGUMENTS if arguments.get(name) != value]
    errors.extend(f"missing exact {package} install" for package in REQUIRED_INSTALLS if package not in packages)
    if arguments.get("CURL_VERSION") != arguments.get("LIBCURL4_VERSION"):
        errors.append("curl and libcurl4 versions must match")
    return tuple(errors)


def test_openclaw_dockerfile_uses_approved_bookworm_package_pins() -> None:
    # Given
    source = DOCKERFILE.read_text(encoding="utf-8")

    # When
    errors = _bookworm_pin_errors(source)

    # Then
    assert errors == ()


def test_bookworm_pin_contract_rejects_stale_or_unpinned_package_changes() -> None:
    # Given
    regression_cases = (
        ("BASH_VERSION=5.2.15-2+b13", "BASH_VERSION=5.2.15-2+b2", "BASH_VERSION"),
        ("CURL_VERSION=7.88.1-10+deb12u15", "CURL_VERSION=7.88.1-10+deb12u14", "CURL_VERSION"),
        ('    "libcurl4=${LIBCURL4_VERSION}" \\\n', "", "libcurl4=${LIBCURL4_VERSION}"),
        ("LIBCURL4_VERSION=7.88.1-10+deb12u15", "LIBCURL4_VERSION=7.88.1-10+deb12u14", "versions must match"),
        ('"curl=${CURL_VERSION}"', '"curl"', "curl=${CURL_VERSION}"),
    )

    for needle, replacement, expected_error in regression_cases:
        candidate = VALID_BOOKWORM_PIN_BLOCK.replace(needle, replacement, 1)
        assert candidate != VALID_BOOKWORM_PIN_BLOCK

        # When
        errors = _bookworm_pin_errors(candidate)

        # Then
        assert any(expected_error in error for error in errors)


@pytest.mark.parametrize("target", ["linux/amd64", "linux/arm64", "linux/386", "darwin/arm64"])
def test_dockerfile_target_selection_uses_buildkit_arguments_not_host_architecture(target: str) -> None:
    # Given
    source = DOCKERFILE.read_text()
    stage = source.split("FROM ${OPENCLAW_BASE_IMAGE}", 1)[1]
    assert "ARG TARGETOS\n" in stage
    assert "ARG TARGETARCH\n" in stage
    selection = source.split("RUN case ", 1)[1].split("esac", 1)[0]
    operating_system, architecture = target.split("/")
    arguments = _argument_values(source)

    # When
    result = subprocess.run(  # noqa: S603 - execute only the target selection, never the download/build.
        ["/bin/sh", "-c", f"case {selection}esac\nprintf '%s' \"$GOGCLI_SHA256\""],
        env={"TARGETOS": operating_system, "TARGETARCH": architecture, **arguments},
        text=True,
        capture_output=True,
        check=False,
    )

    # Then
    if target in {"linux/amd64", "linux/arm64"}:
        expected = {
            "amd64": "sha256:c3ed94efca90c754cdda300e3c7f8cc2f3d7df86d3fdc5d11f996c7518a09c24",
            "arm64": "sha256:90d236ab91c6dae7139bfe0508eab94a8b4323238b62fa29e6cf479a352576f2",
        }
        assert result.returncode == 0, result.stderr
        assert result.stdout == expected[architecture]
    else:
        assert result.returncode != 0
        assert "unsupported guest target" in result.stderr
