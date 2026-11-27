import os

import pytest
from pydantic import ValidationError
from pydantic_settings import BaseSettings

from app.config import Settings

_BOUNDS = {
    "MAX_RUNNING_CHILDREN": (200, 1, 5000),
    "MAX_STARTS_PER_POLL": (5, 1, 100),
    "WORKER_HEAP_MB": (512, 64, 8192),
}
_NAMES = [f"{bridge}_PERSONAL_{suffix}" for bridge in ("WHATSAPP", "ZALO") for suffix in _BOUNDS]


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in tuple(os.environ):
        monkeypatch.delenv(name)


def _settings_without_dotenv() -> Settings:
    # Use the typed base initializer; Pyright's synthesized Settings signature omits _env_file.
    settings = Settings.__new__(Settings)
    BaseSettings.__init__(settings, _env_file=None)
    return settings


def test_fleet_limits_default_to_the_documented_values_when_environment_is_empty() -> None:
    # Given an isolated environment and disabled dotenv loading.
    settings = _settings_without_dotenv()

    # Then each bridge carries the documented ceiling, pace and heap.
    for name in _NAMES:
        assert getattr(settings, name) == _BOUNDS[name.split("_PERSONAL_")[1]][0]


@pytest.mark.parametrize("name", _NAMES)
def test_fleet_limits_accept_their_inclusive_bounds(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    _, low, high = _BOUNDS[name.split("_PERSONAL_")[1]]
    for value in (low, high):
        # Given a value on a bound, when settings parse it, then it is carried unchanged.
        monkeypatch.setenv(name, str(value))
        assert getattr(_settings_without_dotenv(), name) == value


@pytest.mark.parametrize("name", _NAMES)
def test_fleet_limits_reject_values_outside_their_bounds(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    _, low, high = _BOUNDS[name.split("_PERSONAL_")[1]]
    for value in (low - 1, high + 1):
        # Given a value just outside a bound, when settings parse it, then validation fails before a supervisor runs.
        monkeypatch.setenv(name, str(value))
        with pytest.raises(ValidationError):
            _settings_without_dotenv()
