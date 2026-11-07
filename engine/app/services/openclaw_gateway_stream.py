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
class _Snapshot:
    """The one unpublished update; accumulates redacted frames, so mutation is its whole purpose.

    Holds the safe text since the last publication (restarted by a replacement) and the newest merged frame. Text
    stays in a list and is joined once per publication; its size is bounded by the stream text the exec cap admits.
    """

    def __init__(self) -> None:
        self._parts: list[str] = []
        self._latest: GatewayDelta | None = None
        self._replace = False

    @property
    def waiting(self) -> bool:
        return self._latest is not None

    def merge(self, delta: GatewayDelta, safe: str) -> None:
        if delta.replace:
            self._parts.clear()
            self._replace = True
        self._parts.append(safe)
        self._latest = delta

    def take(self) -> GatewayDelta | None:
        """The latest complete safe text with its newest ``seq``, or ``None`` when nothing is waiting."""
        if self._latest is None:
            return None
        taken = self._latest.model_copy(update={"text": "".join(self._parts), "replace": self._replace})
        self._parts.clear()
        self._latest = None
        self._replace = False
        return taken


@final
class DeltaRelay:
    """Own one turn's mutable queue and counters; reserve slots BEFORE scheduling loop callbacks.

    Ingestion (parse, redact, merge) never waits for delivery: one sender publishes the single pending snapshot, so a
    slow path skips obsolete intermediate updates instead of queueing them.
    """

    def __init__(self, message: GatewayMessageRecord, timing: TurnTimeline | None, guard: ResponseGuard | None = None):
        self.message = message
        self.timing = timing
        self.redactor = (guard or ResponseGuard.build([message.guest_model_ref])).stream()
        self.loop = asyncio.get_running_loop()
        self.queue: asyncio.Queue[str | None] = asyncio.Queue(QUEUE_SIZE + 1)
        self.slots = BoundedSemaphore(QUEUE_SIZE)
        self.dropped = 0
        self.pending = _Snapshot()
        self.wake = asyncio.Event()
        self.closed = False

    def offer(self, line: str) -> None:
        if self.slots.acquire(blocking=False):
            self.loop.call_soon_threadsafe(self.queue.put_nowait, line)
        else:
            self.dropped += 1

    def finish(self) -> None:
        # The extra queue slot is reserved for completion, never a delta.
        self.queue.put_nowait(None)

    async def drain(self, consumer: asyncio.Task[None]) -> None:
        """Flush the in-flight and latest pending update before settlement for at most DRAIN_SECONDS.

        The terminal result supersedes anything left. The caller owns ``consumer`` and cancels it afterwards, so an
        expired drain leaves no task behind.
        """
        self.finish()
        done, _ = await asyncio.wait({consumer}, timeout=DRAIN_SECONDS)
        if not done:
            logger.warning(
                "[Gateway] Skipped queued stream deltas message={} queued={} pending={}",
                self.message.id,
                self.queue.qsize(),
                self.pending.waiting,
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
        """Ingest queued deltas and publish them through one sender until completion is signalled."""
        async with asyncio.TaskGroup() as group:
            _ = group.create_task(self._send())
            await self._ingest()
        if self.dropped:
            logger.warning("[Gateway] dropped stream deltas message={} count={}", self.message.id, self.dropped)

    async def _ingest(self) -> None:
        """Redact every admitted frame, then merge its safe output; never awaits delivery."""
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
                self.pending.merge(delta, safe)
                self.wake.set()
        self.closed = True
        self.wake.set()

    async def _send(self) -> None:
        """The only in-flight publication; whatever arrived meanwhile goes out as one latest snapshot."""
        from app.services.openclaw_gateway_delivery import publish_chunk

        while True:
            while (delta := self.pending.take()) is not None:
                started = monotonic()
                try:
                    await publish_chunk(self.message, delta)
                finally:
                    if self.timing is not None:
                        self.timing.chunk_published(monotonic() - started)
            if self.closed:
                return
            self.wake.clear()  # no await since `take` saw nothing, so no merge can slip in before the wait
            await self.wake.wait()
