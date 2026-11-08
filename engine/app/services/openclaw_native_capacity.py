"""Loop-owned native threads and fair, key-only admission queues.

All counters and queues are mutated on the owning loop. Waiting lanes retain no
message payloads and never own tasks, claims, or executor work.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar, copy_context
from dataclasses import dataclass
from typing import assert_never
from uuid import UUID
from weakref import WeakKeyDictionary

from app.config import get_settings
from app.records.agent import AgentRecord
from app.records.gateway_message import GatewayMessageRecord

WAITING_LANES_PER_AGENT = 64


@dataclass(frozen=True, slots=True)
class LaneKey:
    agent_id: UUID
    lane: str | UUID

    @classmethod
    def of(cls, message: GatewayMessageRecord) -> LaneKey:
        return cls(message.agent_id, message.conversation_id or message.id)

    def probe(self) -> GatewayMessageRecord:
        """Supply only lane identity to the existing pending-head read."""
        match self.lane:
            case UUID() as message_id:
                return GatewayMessageRecord(message_id, self.agent_id, "")
            case str() as conversation:
                return GatewayMessageRecord(self.agent_id, self.agent_id, "", conversation_id=conversation)
            case unreachable:
                assert_never(unreachable)


class NativeCapacity:
    """Mutable admission accounting and a lazy dedicated thread pool for one loop."""

    def __init__(self) -> None:
        self.limit: int = get_settings().OPENCLAW_PROCESS_MAX_PARALLEL_TURNS
        self.active: dict[UUID, int] = {}
        self.waiting: dict[UUID, dict[LaneKey, None]] = {}
        self.agents: dict[UUID, AgentRecord] = {}
        self.dirty: set[UUID] = set()
        self.rotation: deque[UUID] = deque()
        self.executor: ThreadPoolExecutor | None = None

    @property
    def free(self) -> int:
        return self.limit - sum(self.active.values())

    def saturated(self, agent_id: UUID) -> bool:
        return self.free <= 0 or self.active.get(agent_id, 0) >= get_settings().OPENCLAW_AGENT_MAX_PARALLEL_TURNS

    def remember(self, agent: AgentRecord) -> None:
        self.agents[agent.id] = agent
        if agent.id not in self.rotation:
            self.rotation.append(agent.id)

    def rescan(self, agent: AgentRecord) -> None:
        self.remember(agent)
        self.dirty.add(agent.id)

    def enqueue(self, agent: AgentRecord, key: LaneKey) -> bool:
        self.remember(agent)
        lanes = self.waiting.setdefault(agent.id, {})
        if key in lanes:
            return True
        if len(lanes) >= WAITING_LANES_PER_AGENT:
            self.dirty.add(agent.id)
            return False
        lanes[key] = None
        return True

    def reserve(self, agent_id: UUID) -> None:
        self.active[agent_id] = self.active.get(agent_id, 0) + 1

    def release(self, agent_id: UUID) -> None:
        remaining = self.active[agent_id] - 1
        if remaining:
            self.active[agent_id] = remaining
        else:
            del self.active[agent_id]

    def next(self, *, scans_allowed: bool) -> tuple[AgentRecord, LaneKey | None] | None:
        """Rotate agents after one turn; a dirty agent gets discovery in the same ring."""
        if self.free <= 0:
            return None
        for _ in range(len(self.rotation)):
            agent_id = self.rotation.popleft()
            lanes = self.waiting.get(agent_id, {})
            dirty = agent_id in self.dirty
            if not lanes and not dirty:
                self.waiting.pop(agent_id, None)
                self.agents.pop(agent_id, None)
                continue
            self.rotation.append(agent_id)
            if self.saturated(agent_id):
                continue
            if lanes:
                key = next(iter(lanes))
                del lanes[key]
                self.reserve(agent_id)
                return self.agents[agent_id], key
            if scans_allowed:
                self.dirty.remove(agent_id)
                self.rotation.pop()
                self.rotation.appendleft(agent_id)
                self.reserve(agent_id)
                return self.agents[agent_id], None
        return None

    def discard_waiting(self) -> None:
        self.waiting.clear()
        self.dirty.clear()
        self.rotation.clear()
        self.agents.clear()

    async def run[**P, R](self, function: Callable[P, R], *args: P.args, **kwargs: P.kwargs) -> R:
        """Match to_thread's context propagation without borrowing default threads."""
        if self.executor is None:
            self.executor = ThreadPoolExecutor(max_workers=self.limit, thread_name_prefix="openclaw-native")
        context = copy_context()
        return await asyncio.get_running_loop().run_in_executor(
            self.executor, lambda: context.run(function, *args, **kwargs)
        )

    def shutdown(self) -> None:
        """Called only after owned turns have joined, including CLI reaping."""
        self.discard_waiting()
        if self.executor is not None:
            self.executor.shutdown(wait=True)
            self.executor = None


_capacities: WeakKeyDictionary[asyncio.AbstractEventLoop, NativeCapacity] = WeakKeyDictionary()
turn_release: ContextVar[Callable[[], None] | None] = ContextVar("native_turn_release", default=None)


def native_capacity() -> NativeCapacity:
    loop = asyncio.get_running_loop()
    capacity = _capacities.get(loop)
    if capacity is None:
        capacity = NativeCapacity()
        _capacities[loop] = capacity
    return capacity


def release_turn() -> None:
    release = turn_release.get()
    if release is not None:
        release()


def shutdown_native_capacity() -> None:
    capacity = _capacities.pop(asyncio.get_running_loop(), None)
    if capacity is not None:
        capacity.shutdown()


def discard_waiting_lanes() -> None:
    capacity = _capacities.get(asyncio.get_running_loop())
    if capacity is not None:
        capacity.discard_waiting()
