"""Idle-wait shortcut for the report relay; PostgreSQL stays the only durable queue.

A wake carries no data. Losing one (Redis down, no worker listening) costs at most the relay's polling interval.
"""

import asyncio
from collections.abc import Generator, Mapping
from contextlib import contextmanager, suppress
from typing import Final
from uuid import uuid4

import anyio

from app.api.background_tasks import schedule_background_task
from app.core.events import get_redis
from app.core.logging import logger

WAKE_CHANNEL: Final = "gateway_report_relay:wake"
_ORIGIN: Final = uuid4().hex
_PUBLISH_TIMEOUT: Final = 2.0
_RESUBSCRIBE_MIN: float = 1.0
_RESUBSCRIBE_MAX: float = 30.0
_listeners: set[asyncio.Event] = set()


@contextmanager
def listen() -> Generator[asyncio.Event]:
    """Register the relay loop's event for the duration of its run."""
    event = asyncio.Event()
    _listeners.add(event)
    try:
        yield event
    finally:
        _listeners.discard(event)


def signal_local() -> None:
    for event in _listeners:
        event.set()


async def _announce() -> None:
    signal_local()
    try:
        async with asyncio.timeout(_PUBLISH_TIMEOUT):
            _ = await (await get_redis()).publish(WAKE_CHANNEL, _ORIGIN)
    except Exception as exc:
        logger.warning("[GatewayReportRelay] wake publish failed; workers fall back to polling: {}", exc)


def wake_gateway_report_relay() -> None:
    """Call after creating a claimable report intent; the signal fires once the transaction commits."""
    schedule_background_task(_announce(), "wake gateway report relay")


def _is_foreign(message: Mapping[str, object] | None) -> bool:
    if message is None:
        return False
    data = message.get("data")
    return (data.decode(errors="replace") if isinstance(data, bytes) else data) != _ORIGIN


async def subscribe_wakes() -> None:
    """Forward wakes published by other processes; reconnect with backoff and never raise."""
    delay = _RESUBSCRIBE_MIN
    degraded = False
    while True:
        pubsub = None
        try:
            pubsub = (await get_redis()).pubsub()
            await pubsub.subscribe(WAKE_CHANNEL)
            if degraded:
                logger.info("[GatewayReportRelay] wake subscription restored")
            degraded = False
            delay = _RESUBSCRIBE_MIN
            signal_local()  # wakes published while unsubscribed were missed; poll once now
            while True:
                if _is_foreign(await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)):
                    signal_local()
        except Exception as exc:
            (logger.debug if degraded else logger.warning)(
                "[GatewayReportRelay] wake subscription unavailable; polling only: {}", exc
            )
            degraded = True
        finally:
            if pubsub is not None:
                with suppress(Exception):
                    await pubsub.aclose()
        await anyio.sleep(delay)
        delay = min(delay * 2, _RESUBSCRIBE_MAX)
