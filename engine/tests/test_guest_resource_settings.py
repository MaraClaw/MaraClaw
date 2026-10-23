import os

import pytest
from pydantic import ValidationError
from pydantic_settings import BaseSettings

from app.config import Settings


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in tuple(os.environ):
        monkeypatch.delenv(name)


def _settings_without_dotenv() -> Settings:
    # Use the typed base initializer; Pyright's synthesized Settings signature omits _env_file.
    settings = Settings.__new__(Settings)
    BaseSettings.__init__(settings, _env_file=None)
    return settings


def test_guest_resource_limits_when_environment_is_empty() -> None:
    # Given an isolated environment and disabled dotenv loading.
    # When settings are constructed without overrides.
    settings = _settings_without_dotenv()

    # Then the per-guest trial defaults are used.
    assert (
        settings.OPENCLAW_GUEST_MEMORY_LIMIT_BYTES,
        settings.OPENCLAW_GUEST_CPUS,
        settings.OPENCLAW_GUEST_PIDS_LIMIT,
    ) == (2147483648, 1.0, 512)


def test_guest_resource_limits_when_environment_overrides_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given nondefault limits, including a fractional CPU allocation.
    monkeypatch.setenv("OPENCLAW_GUEST_MEMORY_LIMIT_BYTES", "1073741824")
    monkeypatch.setenv("OPENCLAW_GUEST_CPUS", "0.75")
    monkeypatch.setenv("OPENCLAW_GUEST_PIDS_LIMIT", "256")

    # When settings parse the isolated environment without dotenv files.
    settings = _settings_without_dotenv()

    # Then each environment value overrides its default with the parsed value.
    assert (
        settings.OPENCLAW_GUEST_MEMORY_LIMIT_BYTES,
        settings.OPENCLAW_GUEST_CPUS,
        settings.OPENCLAW_GUEST_PIDS_LIMIT,
    ) == (1073741824, 0.75, 256)


@pytest.mark.parametrize(
    ("setting", "value"),
    [
        ("OPENCLAW_GUEST_MEMORY_LIMIT_BYTES", "0"),
        ("OPENCLAW_GUEST_MEMORY_LIMIT_BYTES", "-1"),
        ("OPENCLAW_GUEST_CPUS", "0"),
        ("OPENCLAW_GUEST_CPUS", "-0.5"),
        ("OPENCLAW_GUEST_PIDS_LIMIT", "0"),
        ("OPENCLAW_GUEST_PIDS_LIMIT", "-1"),
    ],
)
def test_guest_resource_limits_reject_nonpositive_environment_values(
    monkeypatch: pytest.MonkeyPatch, setting: str, value: str
) -> None:
    # Given one nonpositive resource limit in an otherwise isolated environment.
    monkeypatch.setenv(setting, value)

    # When settings parse that limit without dotenv files.
    with pytest.raises(ValidationError) as exc_info:
        _settings_without_dotenv()

    # Then only the specified limit fails its positive-value constraint.
    assert exc_info.value.error_count() == 1
    error = exc_info.value.errors()[0]
    assert error["loc"] == (setting,)
    assert error["type"] == "greater_than"
