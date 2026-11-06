import asyncio
import json
from contextlib import suppress
from itertools import batched
from uuid import uuid4

import pytest

from app.api.websocket import manager
from app.config import get_settings
from app.records.gateway_message import GatewayMessageRecord
from app.services import openclaw_gateway_stream as streaming

REF = "openrouter/nvidia/nemotron-3.5-super-vl:free"
BRAND = get_settings().APP_NAME


def delta_line(message: GatewayMessageRecord, seq: int, text: str, replace: bool = False) -> str:
    return json.dumps({"kind": "delta", "messageId": str(message.id), "seq": seq, "text": text, "replace": replace})


class IngestProbe:
    """Reports each frame the relay ingests; the real redactor still decides what is safe."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, relay: streaming.DeltaRelay) -> None:
        self.ingested_seqs: asyncio.Queue[int] = asyncio.Queue()
        feed = relay.redactor.feed

        def observe(seq: int, text: str, replace: bool) -> str | None:
            self.ingested_seqs.put_nowait(seq)
            return feed(seq, text, replace)

        monkeypatch.setattr(relay.redactor, "feed", observe)

    async def ingested(self, seq: int) -> None:
        try:
            while await asyncio.wait_for(self.ingested_seqs.get(), 5) != seq:
                pass
        except TimeoutError:
            pytest.fail(f"delta {seq} was not ingested while the first publication was still in flight")


class SlowFirstSend(IngestProbe):
    """Keeps the first socket send in flight until released."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, relay: streaming.DeltaRelay) -> None:
        super().__init__(monkeypatch, relay)
        self.sent: list[tuple[int, str, bool]] = []
        self.in_flight = asyncio.Event()
        self.release = asyncio.Event()

        async def send(_agent_id, _conversation_id, event, *, user_id, send_timeout=None):
            self.sent.append((event["seq"], event["content"], event["replace"]))
            if len(self.sent) == 1:
                self.in_flight.set()
                await self.release.wait()

        monkeypatch.setattr(manager, "send_to_session", send)


@pytest.mark.parametrize("snapshot", [False, True])
async def test_blocked_publication_catches_up_with_one_latest_snapshot(monkeypatch, snapshot):
    message = GatewayMessageRecord(uuid4(), uuid4(), "hello", conversation_id=str(uuid4()))
    relay = streaming.DeltaRelay(message, None)
    wire = SlowFirstSend(monkeypatch, relay)
    # More updates than the 64 reserved slots, offered in batches that fit them, like a producer the loop keeps up with.
    frames = [(seq, f"{seq},", False) for seq in range(1, 151)]
    if snapshot:
        frames[99] = (100, "snapshot ", True)
    kept = frames[99:] if snapshot else frames
    consumer = asyncio.create_task(relay.publish())
    try:
        relay.offer(delta_line(message, 0, "first "))
        await asyncio.wait_for(wire.in_flight.wait(), 5)

        for batch in batched(frames, 32, strict=False):
            for seq, text, replace in batch:
                relay.offer(delta_line(message, seq, text, replace))
            await wire.ingested(batch[-1][0])
        assert relay.dropped == 0
        assert relay.queue.empty()

        wire.release.set()
        await asyncio.wait_for(relay.drain(consumer), 5)
        assert consumer.done()
        await consumer
    finally:
        consumer.cancel()
        with suppress(asyncio.CancelledError):
            await consumer

    assert wire.sent == [(0, "first ", False), (150, "".join(text for _, text, _ in kept), snapshot)]


async def test_split_model_id_in_coalesced_frames_is_redacted_frame_by_frame(monkeypatch):
    message = GatewayMessageRecord(uuid4(), uuid4(), "hello", conversation_id=str(uuid4()), guest_model_ref=REF)
    relay = streaming.DeltaRelay(message, None)
    wire = SlowFirstSend(monkeypatch, relay)
    pieces = ["Running on openrouter/nv", "idia/nemotron-3.5-su", "per-vl:fr", "ee, ok."]
    consumer = asyncio.create_task(relay.publish())
    try:
        relay.offer(delta_line(message, 0, "Hello "))
        await asyncio.wait_for(wire.in_flight.wait(), 5)
        for seq, piece in enumerate(pieces, start=1):
            relay.offer(delta_line(message, seq, piece))
        await wire.ingested(len(pieces))

        wire.release.set()
        await asyncio.wait_for(relay.drain(consumer), 5)
        await consumer
    finally:
        consumer.cancel()
        with suppress(asyncio.CancelledError):
            await consumer

    assert wire.sent == [(0, "Hello ", False), (4, f"Running on {BRAND}, ok.", False)]
    assert not [c for _, c, _ in wire.sent if any(word in c.lower() for word in ("openrouter", "nvidia", "nemotron"))]


async def test_gap_after_a_dropped_delta_publishes_nothing_even_while_a_publication_is_in_flight(monkeypatch):
    message = GatewayMessageRecord(uuid4(), uuid4(), "hello", conversation_id=str(uuid4()), guest_model_ref=REF)
    relay = streaming.DeltaRelay(message, None)
    wire = SlowFirstSend(monkeypatch, relay)
    consumer = asyncio.create_task(relay.publish())
    try:
        relay.offer(delta_line(message, 0, "safe "))
        await asyncio.wait_for(wire.in_flight.wait(), 5)
        relay.offer(
            delta_line(message, 2, "uper-vl:free tail")
        )  # seq 1 was dropped: nothing may follow until a snapshot
        await wire.ingested(2)

        wire.release.set()
        await asyncio.wait_for(relay.drain(consumer), 5)
        await consumer
    finally:
        consumer.cancel()
        with suppress(asyncio.CancelledError):
            await consumer

    assert wire.sent == [(0, "safe ", False)]
