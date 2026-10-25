"""Reopen trigger, heartbeat and inbox-wake admission after tests that run a real ``Runtime.stop``."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from app.services import heartbeat, openclaw_routing, trigger_daemon


def reopen_producer_admission() -> None:
    trigger_daemon.open_trigger_admission()
    heartbeat.open_heartbeat_admission()
    openclaw_routing.open_wake_admission()


@pytest.fixture(autouse=True)
def producer_admission() -> Iterator[None]:
    reopen_producer_admission()
    try:
        yield
    finally:
        reopen_producer_admission()
