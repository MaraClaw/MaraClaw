"""Freeze the clock the per-agent daily message cap reads, so tests never straddle a local midnight."""

from datetime import UTC, datetime, tzinfo
from typing import Self, override

import pytest

from app.services import agent_message_quota

NOW = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)
TODAY = NOW.date()
# Fourteen hours ahead of UTC, so at NOW its calendar date (the 14th) is not the UTC date (the 13th).
FAR_EAST = "Etc/GMT-14"


class _FrozenDatetime(datetime):
    @classmethod
    @override
    def now(cls, tz: tzinfo | None = None) -> Self:
        return cls.fromtimestamp(NOW.timestamp(), tz)


@pytest.fixture(autouse=True)
def frozen_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(agent_message_quota, "datetime", _FrozenDatetime)
