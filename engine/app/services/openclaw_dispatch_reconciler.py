"""Worker-owned recovery for pending OpenClaw lane heads."""

from datetime import datetime
from uuid import UUID

import anyio

from app.core.logging import logger
from app.dao.agent_dao import agent_dao
from app.dao.gateway_message_dao import gateway_message_dao
from app.services.openclaw_routing import wake_shared_lane


class OpenClawDispatchReconciler:
    def __init__(self) -> None:
        self._after: tuple[datetime, UUID] | None = None

    async def scan_once(self) -> int:
        messages, self._after = await gateway_message_dao.pending_dispatch_page(50, after=self._after)
        agents = {agent.id: agent for agent in await agent_dao.get_many(list({msg.agent_id for msg in messages}))}
        count = 0
        for message in messages:
            agent = agents.get(message.agent_id)
            if agent is not None:
                wake_shared_lane(agent, message.id)
                count += 1
        return count

    async def run(self) -> None:
        while True:
            started = anyio.current_time()
            try:
                await self.scan_once()
            except Exception:
                logger.warning("[OpenClaw] dispatch recovery scan failed code=scan_failed")
            await anyio.sleep(max(0.0, 1.0 - (anyio.current_time() - started)))


async def openclaw_dispatch_reconciler_loop() -> None:
    await OpenClawDispatchReconciler().run()
