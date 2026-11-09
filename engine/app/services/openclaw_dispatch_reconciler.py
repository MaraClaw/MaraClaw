"""Worker-owned recovery for pending lane heads and conclusive delivered-turn evidence."""

import sqlite3
from datetime import datetime
from uuid import UUID

import anyio

from app.core.logging import logger
from app.dao.agent_dao import agent_dao
from app.dao.gateway_message_dao import gateway_message_dao
from app.records.agent import AgentRecord
from app.records.gateway_message import GatewayMessageRecord
from app.runtime.tasks import loop_progress
from app.services import openclaw_gateway_delivery as delivery
from app.services.openclaw_interrupted_inputs import interrupted_before_consumption
from app.services.openclaw_recovered_dispatch import settle_recovered_report
from app.services.openclaw_routing import wake_shared_lane
from app.services.openclaw_turns import runtime_session_key

PAGE = 50
PROGRESS_BUDGET_SECONDS = 300.0
INTERRUPTED_NOTICE = "The agent was interrupted before it could start this request. Please send it again."


async def _interrupted(agent: AgentRecord, message: GatewayMessageRecord) -> bool:
    from app.services.agent_manager import agent_manager

    try:
        return await anyio.to_thread.run_sync(
            interrupted_before_consumption, agent_manager._agent_dir(agent.id), runtime_session_key(message), message.id
        )
    except OSError, sqlite3.Error:
        logger.warning("[OpenClaw] interrupted input read failed agent={} code=guest_read_failed", agent.id)
        raise


class OpenClawDispatchReconciler:
    def __init__(self) -> None:
        self._after: tuple[datetime, UUID] | None = None
        self._delivered_after: tuple[datetime, UUID] | None = None

    async def _wake_pending(self) -> int:
        messages, after = await gateway_message_dao.pending_dispatch_page(PAGE, after=self._after)
        agents = {agent.id: agent for agent in await agent_dao.get_many(list({msg.agent_id for msg in messages}))}
        count = 0
        for message in messages:
            agent = agents.get(message.agent_id)
            if agent is not None:
                wake_shared_lane(agent, message.id)
                count += 1
        self._after = after
        return count

    async def _settle_interrupted(self) -> int:
        """Complete delivered turns with exact recovery evidence; never release or resubmit delivered input."""
        messages, after = await gateway_message_dao.delivered_dispatch_page(PAGE, after=self._delivered_after)
        agents = {agent.id: agent for agent in await agent_dao.get_many(list({msg.agent_id for msg in messages}))}
        count = 0
        for message in messages:
            agent = agents.get(message.agent_id)
            binding_id = message.reply_binding_id
            if agent is None or binding_id is None:
                continue
            recovered = await settle_recovered_report(agent, message)
            if recovered is not None:
                await delivery.publish_report(agent, recovered)
                count += 1
                continue
            if not await _interrupted(agent, message):
                continue
            report = await delivery.settle_native_report(agent, message.id, INTERRUPTED_NOTICE, binding_id=binding_id)
            if report is not None:
                await delivery.publish_report(agent, report)
                count += 1
        self._delivered_after = after
        return count

    async def scan_once(self) -> int:
        return await self._wake_pending() + await self._settle_interrupted()

    async def run(self) -> None:
        while True:
            started = anyio.current_time()
            try:
                await self.scan_once()
            except Exception:
                logger.warning("[OpenClaw] dispatch recovery scan failed code=scan_failed")
            else:
                loop_progress().beat()
            await anyio.sleep(max(0.0, 1.0 - (anyio.current_time() - started)))


async def openclaw_dispatch_reconciler_loop() -> None:
    await OpenClawDispatchReconciler().run()
