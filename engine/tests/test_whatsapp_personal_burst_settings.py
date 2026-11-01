import os

import pytest
from pydantic import ValidationError
from pydantic_settings import BaseSettings

from app.config import Settings
from app.records.personal_burst import PersonalBurstLimits
from app.services.channels.personal_burst_limits import personal_burst_limits

_PREFIX = "WHATSAPP_PERSONAL_BURST_"
_QUIET, _MAX = _PREFIX + "QUIET_SECONDS", _PREFIX + "MAX_SECONDS"
_COUNT, _PENDING, _BODY = _PREFIX + "PENDING_COUNT", _PREFIX + "PENDING_BYTES", _PREFIX + "BODY_BYTES"
_COOLDOWN = "WHATSAPP_PERSONAL_BUSY_NOTICE_COOLDOWN_SECONDS"


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in tuple(os.environ):
        monkeypatch.delenv(name)


def _settings_without_dotenv() -> Settings:
    # Use the typed base initializer; Pyright's synthesized Settings signature omits _env_file.
    settings = Settings.__new__(Settings)
    BaseSettings.__init__(settings, _env_file=None)
    return settings


def test_burst_limits_when_environment_is_empty() -> None:
    # Given an isolated environment and disabled dotenv loading.
    # When settings are projected to the persistence limits.
    limits = personal_burst_limits(_settings_without_dotenv())

    # Then the contract defaults apply.
    assert limits == PersonalBurstLimits(
        quiet_seconds=2.0,
        max_seconds=5.0,
        pending_count=50,
        pending_bytes=65536,
        batch_bytes=16384,
        notice_cooldown_seconds=30.0,
    )


def test_burst_limits_when_environment_overrides_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given every limit overridden with a valid nondefault value.
    for name, value in (
        (_QUIET, "0.5"),
        (_MAX, "3"),
        (_COUNT, "7"),
        (_PENDING, "2048"),
        (_BODY, "1024"),
        (_COOLDOWN, "12.5"),
    ):
        monkeypatch.setenv(name, value)

    # When settings parse the isolated environment.
    limits = personal_burst_limits(_settings_without_dotenv())

    # Then each value is carried into the limits unchanged.
    assert (
        limits.quiet_seconds,
        limits.max_seconds,
        limits.pending_count,
        limits.pending_bytes,
        limits.batch_bytes,
        limits.notice_cooldown_seconds,
    ) == (0.5, 3.0, 7, 2048, 1024, 12.5)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        (_QUIET, "0"),
        (_QUIET, "-1"),
        (_QUIET, "nan"),
        (_QUIET, "inf"),
        (_MAX, "0"),
        (_MAX, "nan"),
        (_MAX, "inf"),
        (_COUNT, "0"),
        (_PENDING, "0"),
        (_BODY, "0"),
        (_COOLDOWN, "0"),
        (_COOLDOWN, "nan"),
        (_COOLDOWN, "inf"),
    ],
)
def test_burst_settings_reject_nonpositive_or_nonfinite_values(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    # Given a nonpositive or non-finite limit.
    monkeypatch.setenv(name, value)

    # When settings parse it, then validation fails before any admission can use it.
    with pytest.raises(ValidationError):
        _settings_without_dotenv()


@pytest.mark.parametrize(
    "overrides",
    [
        {_QUIET: "6", _MAX: "5"},
        {_PENDING: "1000", _BODY: "1001"},
    ],
)
def test_burst_settings_reject_inconsistent_windows_and_byte_limits(
    monkeypatch: pytest.MonkeyPatch, overrides: dict[str, str]
) -> None:
    # Given quiet above max, or a batch body limit above the pending byte backlog.
    for name, value in overrides.items():
        monkeypatch.setenv(name, value)

    # When settings parse them, then the inconsistency is refused.
    with pytest.raises(ValidationError):
        _settings_without_dotenv()
