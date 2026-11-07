"""Bounded concurrent delivery of one realtime event to the local websockets of an agent."""

import asyncio
import json
from collections.abc import Awaitable, Callable, Mapping, Sequence

LOCAL_FANOUT_CONCURRENCY = 8


def encode_event_text(message: Mapping[str, object]) -> str:
    """Serialize an event once, exactly as ``WebSocket.send_json`` does, so every socket sends the same frame."""
    return json.dumps(message, separators=(",", ":"), ensure_ascii=False)


async def fan_out[T](items: Sequence[T], deliver: Callable[[T], Awaitable[bool]]) -> int:
    """Run ``deliver`` for every item with at most ``LOCAL_FANOUT_CONCURRENCY`` in flight; count the ``True`` results.

    The workers live inside this call, so nothing outlives the delivery and the next event cannot overtake it.
    ``deliver`` owns its failures: it must return ``False`` rather than raise, so one socket never stops the rest.
    """
    remaining = iter(items)
    delivered = 0

    async def worker() -> None:
        nonlocal delivered
        for item in remaining:
            if await deliver(item):
                delivered += 1

    async with asyncio.TaskGroup() as group:
        for _ in range(min(LOCAL_FANOUT_CONCURRENCY, len(items))):
            _ = group.create_task(worker())
    return delivered
