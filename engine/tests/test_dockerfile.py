from pathlib import Path
from typing import Final

DOCKERFILE: Final = Path(__file__).resolve().parents[1] / "Dockerfile"
PRODUCTION_STAGE: Final = "FROM python:3.14.6-slim-trixie AS production"
APT_INSTALL: Final = "apt-get install -y --no-install-recommends"
APPROVED_DIRECT_PINS: Final = (
    "curl=8.14.1-2+deb13u4",
    "chromium=151.0.7922.137-1~deb13u1",
)
REGRESSION_CASES: Final = (
    ("curl=8.14.1-2+deb13u4", "curl=8.14.1-2+deb13u3", "stale curl pin"),
    ("chromium=151.0.7922.137-1~deb13u1", "chromium=150.0.7871.181-1~deb13u1", "stale Chromium pin"),
    ("curl=8.14.1-2+deb13u4", "", "missing curl direct entry"),
    ("curl=8.14.1-2+deb13u4", "curl", "unpinned curl direct entry"),
    (
        "curl=8.14.1-2+deb13u4",
        f"{APPROVED_DIRECT_PINS[0]} {chr(92)}{chr(10)}        {APPROVED_DIRECT_PINS[0]}",
        "duplicate curl direct entry",
    ),
    (
        "chromium=151.0.7922.137-1~deb13u1",
        "chromium=151.0.7922.137-1",
        "non-security Chromium replacement",
    ),
)


def _production_direct_apt_entries(source: str) -> tuple[str, ...]:
    production_stage = source.partition(PRODUCTION_STAGE)[2].partition("\nFROM ")[0]
    apt_install = production_stage.partition(APT_INSTALL)[2]
    entries: list[str] = []
    for raw_line in apt_install.splitlines()[1:]:
        entry = raw_line.strip().removesuffix("\\").strip().removesuffix("&&").strip()
        if entry.startswith("fc-cache"):
            break
        if "=" in entry and not entry.startswith(('"', "'")):
            entries.append(entry)
    return tuple(entries)


def _production_direct_pins(source: str) -> tuple[str, ...]:
    return tuple(
        entry for entry in _production_direct_apt_entries(source) if entry.partition("=")[0] in {"curl", "chromium"}
    )


def test_production_dockerfile_pins_approved_direct_security_packages() -> None:
    # Given
    content = DOCKERFILE.read_text(encoding="utf-8")

    # When
    direct_pins = _production_direct_pins(content)

    # Then
    assert direct_pins == APPROVED_DIRECT_PINS


def test_production_dockerfile_rejects_direct_package_pin_regressions() -> None:
    # Given
    source = (
        DOCKERFILE.read_text(encoding="utf-8")
        .replace("curl=8.14.1-2+deb13u3", APPROVED_DIRECT_PINS[0], 1)
        .replace("chromium=150.0.7871.181-1~deb13u1", APPROVED_DIRECT_PINS[1], 1)
    )
    for needle, replacement, regression in REGRESSION_CASES:
        candidate = source.replace(needle, replacement, 1)
        assert candidate != source, regression

        # When
        direct_pins = _production_direct_pins(candidate)

        # Then
        assert direct_pins != APPROVED_DIRECT_PINS, regression
