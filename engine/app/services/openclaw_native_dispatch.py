"""Task ownership for admitted native lanes and coalesced agent discovery."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime
from uuid import UUID

from app.core.logging import logger
from app.dao.gateway_message_dao import gateway_message_dao
from app.records.agent import AgentRecord
from app.records.gateway_message import GatewayMessageRecord
from app.runtime.tasks import TaskAdmission
from app.services.openclaw_native_capacity import LaneKey, native_capacity, turn_release
from app.services.openclaw_release_backoff import release_backoff


class NativeDispatcher:
    def __init__(self, run: Callable[[AgentRecord, str, UUID], Awaitable[None]], page_size: Callable[[], int]) -> None:
        self.run: Callable[[AgentRecord, str, UUID], Awaitable[None]] = run
        self.page_size: Callable[[], int] = page_size
        self.tasks: set[asyncio.Task[None]] = set()
        self.admission: TaskAdmission = TaskAdmission("Inbox wake", self.tasks)
        self.scanning: set[UUID] = set()
        self.scan_again: set[UUID] = set()
        self.running: set[tuple[UUID, str | UUID]] = set()
        self.again: set[tuple[UUID, str | UUID]] = set()

    def accepting(self) -> bool:
        return self.admission.accepting

    def start(self, agent: AgentRecord, pending: GatewayMessageRecord) -> bool:
        if not self.admission.accepting:
            return False
        lane = LaneKey.of(pending)
        if release_backoff.waiting(lane):
            return True
        key = (agent.id, lane.lane)
        if key in self.running:
            self.again.add(key)
            return True
        accepted = native_capacity().enqueue(agent, lane)
        self.pump()
        return accepted

    def pump(self) -> None:
        if not self.admission.accepting:
            return
        capacity = native_capacity()
        while (item := capacity.next(scans_allowed=len(self.scanning) < capacity.free)) is not None:
            agent, lane = item
            if lane is None:
                self.scan(agent, reserved=True)
            else:
                self.admit(agent, lane)

    def admit(self, agent: AgentRecord, lane: LaneKey) -> None:
        key = (agent.id, lane.lane)
        capacity = native_capacity()
        self.running.add(key)
        held = True

        def release() -> None:
            nonlocal held
            if held:
                held = False
                capacity.release(agent.id)
                self.pump()

        async def run_lane() -> None:
            token = turn_release.set(release)
            followup = False
            try:
                row = await gateway_message_dao.lane_head(lane.probe())
                if row is not None and self.accepting() and not release_backoff.waiting(LaneKey.of(row)):
                    await self.run(agent, row.content, row.id)
                    release()
                    if self.accepting():
                        head = await gateway_message_dao.lane_head(lane.probe())
                        followup = head is not None and head.id != row.id
            except Exception:
                logger.warning("[OpenClaw] native lane failed agent={} code=drain_failed", agent.id)
            finally:
                self.running.discard(key)
                again = key in self.again
                self.again.discard(key)
                if (again or followup) and self.admission.accepting:
                    capacity.enqueue(agent, lane)
                release()
                turn_release.reset(token)
                self.pump()

        task = asyncio.create_task(run_lane())

        def finished(_task: asyncio.Task[None]) -> None:
            if held:
                self.running.discard(key)
                self.again.discard(key)
                release()

        task.add_done_callback(finished)
        self.track(task)

    def wake(self, agent: AgentRecord) -> None:
        native_capacity().rescan(agent)
        self.pump()

    def scan(self, agent: AgentRecord, *, reserved: bool = False) -> None:
        capacity = native_capacity()
        if agent.id in self.scanning:
            self.scan_again.add(agent.id)
            if reserved:
                capacity.release(agent.id)
            return
        self.scanning.add(agent.id)
        started = False

        async def run_scan() -> None:
            nonlocal started
            started = True
            try:
                while True:
                    self.scan_again.discard(agent.id)
                    attempted: set[str | UUID] = set()
                    after: tuple[datetime, UUID] | None = None
                    while self.accepting():
                        heads, after = await gateway_message_dao.lane_heads_page(
                            agent.id, self.page_size(), after=after
                        )
                        for pending in heads:
                            if not self.accepting():
                                return
                            lane = pending.conversation_id or pending.id
                            if lane in attempted:
                                continue
                            attempted.add(lane)
                            if not self.start(agent, pending):
                                return
                        if after is None:
                            break
                    if agent.id not in self.scan_again or not self.admission.accepting:
                        break
                    if native_capacity().saturated(agent.id):
                        native_capacity().rescan(agent)
                        break
            except Exception:
                logger.warning("[OpenClaw] native drain failed agent={} code=drain_failed", agent.id)
            finally:
                self.scanning.discard(agent.id)
                self.scan_again.discard(agent.id)
                if reserved:
                    capacity.release(agent.id)
                self.pump()

        task = asyncio.create_task(run_scan())

        def finished(_task: asyncio.Task[None]) -> None:
            if not started:
                self.scanning.discard(agent.id)
                self.scan_again.discard(agent.id)
                if reserved:
                    capacity.release(agent.id)
                self.pump()

        task.add_done_callback(finished)
        self.track(task)

    def track(self, task: asyncio.Task[None]) -> None:
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
