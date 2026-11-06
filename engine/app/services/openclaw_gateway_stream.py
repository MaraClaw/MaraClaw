"""Bounded thread-to-loop handoff for best-effort native reply deltas."""

import asyncio
from threading import BoundedSemaphore, Event
from time import monotonic
from typing import ClassVar, Literal, final
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, NonNegativeInt, ValidationError
from pywhales import InspectionPolicy, observation

from app.core.logging import logger
from app.dao.chat_dao import chat_session_dao
from app.dao.gateway_report_delivery_dao import gateway_report_delivery_dao
from app.dao.personal_burst_dispatch_dao import personal_burst_dispatch_dao
from app.records.gateway_message import GatewayMessageRecord
from app.services.guest_bootstrap import guarded_argv
from app.services.response_identity import ResponseGuard
from app.services.shared_chat import shared_session_id
from app.services.turn_timing import TurnTimeline

EXEC_POLICY = InspectionPolicy(
    max_references=16, max_argv_bytes=32768, max_response_bytes=4_194_304, max_total_stream_bytes=4_194_304
)
QUEUE_SIZE = 64
DRAIN_SECONDS = 2.0


class GatewayDelta(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True, strict=True)
    kind: Literal["delta"]
    message_id: str = Field(alias="messageId")
    seq: NonNegativeInt
    text: str
    replace: bool = False


async def streams_to_web(message: GatewayMessageRecord) -> bool:
    """Read origin before dispatch; a failed classification read must propagate."""
    if message.conversation_id is None or message.conversation_id == "":
        return False
    if message.conversation_id == str(shared_session_id(message.agent_id)):
        # Claim overwrites routing_reason. The retained header is the durable Personal origin.
        if await personal_burst_dispatch_dao.get(message.id) is not None:
            return False
        return not await gateway_report_delivery_dao.has_im_origin(message.id)
    try:
        session_id = UUID(message.conversation_id)
    except ValueError:
        return False
    session = await chat_session_dao.get(session_id)
    return session is not None and session.agent_id == message.agent_id and session.source_channel == "web"


@final
class DeltaRelay:
    """Own one turn's mutable queue and counters; reserve slots BEFORE scheduling loop callbacks."""

    def __init__(self, message: GatewayMessageRecord, timing: TurnTimeline | None, guard: ResponseGuard | None = None):
        self.message = message
        self.timing = timing
        self.redactor = (guard or ResponseGuard.build([message.guest_model_ref])).stream()
        self.loop = asyncio.get_running_loop()
        self.queue: asyncio.Queue[str | None] = asyncio.Queue(QUEUE_SIZE + 1)
        self.slots = BoundedSemaphore(QUEUE_SIZE)
        self.dropped = 0

    def offer(self, line: str) -> None:
        if self.slots.acquire(blocking=False):
            self.loop.call_soon_threadsafe(self.queue.put_nowait, line)
        else:
            self.dropped += 1

    def finish(self) -> None:
        # The extra queue slot is reserved for completion, never a delta.
        self.queue.put_nowait(None)

    async def drain(self, consumer: asyncio.Task[None]) -> None:
        """Flush queued deltas before settlement for at most DRAIN_SECONDS; the terminal result supersedes the rest.

        The caller owns ``consumer`` and cancels it afterwards, so an expired drain leaves no task behind.
        """
        self.finish()
        done, _ = await asyncio.wait({consumer}, timeout=DRAIN_SECONDS)
        if not done:
            logger.warning(
                "[Gateway] Skipped queued stream deltas message={} queued={}", self.message.id, self.queue.qsize()
            )

    def execute(self, container_id: str, payload_name: str, cancel: Event) -> str:
        from app.services.agent_manager import agent_manager

        client = agent_manager.docker_client
        if client is None:
            raise RuntimeError("Docker client unavailable")
        argv = guarded_argv(
            ["node", "/home/node/custom-state/.maraclaw-rpc.mjs", f"/home/node/custom-state/{payload_name}"]
        )
        pending = bytearray()
        last = ""
        with (
            observation(EXEC_POLICY, deadline=monotonic() + 940, cancel=cancel),
            client.container.execute(container_id, argv, stream=True, timeout=940) as output,
        ):
            for source, chunk in output:
                if source != "stdout":
                    continue
                pending.extend(chunk)
                while (end := pending.find(b"\n")) >= 0:
                    line = bytes(pending[:end]).decode("utf-8", errors="replace")
                    del pending[: end + 1]
                    if line.strip():
                        last = line
                        self.offer(line)
        # The final outcome never passes through the droppable queue.
        return pending.decode("utf-8", errors="replace") if pending.strip() else last

    async def publish(self) -> None:
        from app.services.openclaw_gateway_delivery import publish_chunk

        previous = -1
        while (line := await self.queue.get()) is not None:
            self.slots.release()
            try:
                delta = GatewayDelta.model_validate_json(line)
            except ValidationError:
                continue
            if delta.message_id != str(self.message.id) or delta.seq <= previous:
                continue
            previous = delta.seq
            if self.timing is not None and "first_delta" not in self.timing.marks:
                self.timing.mark("first_delta")
            safe = self.redactor.feed(delta.seq, delta.text, delta.replace)
            if safe is not None:
                await publish_chunk(self.message, delta.model_copy(update={"text": safe}))
        if self.dropped:
            logger.warning("[Gateway] dropped stream deltas message={} count={}", self.message.id, self.dropped)
