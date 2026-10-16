from collections.abc import Sequence
from datetime import datetime

from app.api.background_tasks import schedule_background_task
from app.dao.agent_dao import agent_dao
from app.records.agent import AgentRecord
from app.services.agent_manager import agent_manager


async def stop_containers(agents: Sequence[AgentRecord]) -> None:
    for agent in agents:
        if agent.container_id not in (None, ""):
            _ = await agent_manager.stop_container(agent)


def schedule_container_stops(agents: Sequence[AgentRecord]) -> None:
    if agents:
        schedule_background_task(stop_containers(agents), description="stop disabled agent containers")


async def reconcile_stopped_containers(now: datetime) -> None:
    agents = await agent_dao.prepare_container_stops(now)
    await stop_containers(agents)
