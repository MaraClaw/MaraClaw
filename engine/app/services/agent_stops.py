import shutil
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime

from anyio import Path as AsyncPath
from python_on_whales import ClientNotFoundError
from python_on_whales.exceptions import DockerException

from app.api.background_tasks import schedule_background_task
from app.core.logging import logger
from app.core.row_memo import memo_drop
from app.dao.agent_dao import agent_dao
from app.records.agent import AgentRecord
from app.services.agent_deletion_history import snapshot_task_history
from app.services.agent_manager import agent_manager
from app.services.agent_orphans import sweep_orphan_containers
from app.services.agent_setup_lock import drain_on_cancel, setup_workspace_lock
from app.services.guest_identity import GuestOwnershipError


@dataclass(frozen=True, slots=True)
class AgentTeardown:
    agent: AgentRecord
    task_history: str | None


async def snapshot_agent_teardown(agents: Sequence[AgentRecord]) -> tuple[AgentTeardown, ...]:
    return tuple([AgentTeardown(replace(agent), await snapshot_task_history(agent.id)) for agent in agents])


async def _teardown_deleted_agents(snapshots: Sequence[AgentTeardown]) -> None:
    for snapshot in snapshots:
        agent = snapshot.agent
        try:
            async with setup_workspace_lock(agent_manager._agent_dir(agent.id)):
                # Under the lock an in-flight OpenClaw cutover has already published or rolled back.
                try:
                    _ = await agent_manager.remove_container(agent)
                except (ClientNotFoundError, DockerException, GuestOwnershipError, OSError) as exc:
                    logger.warning("Deleted agent container cleanup failed agent_id=%s: %s", agent.id, exc)
                archive_dir = await agent_manager.archive_agent_files(agent.id)
                if snapshot.task_history is not None:
                    _ = await AsyncPath(archive_dir / "task_history.json").write_text(
                        snapshot.task_history, encoding="utf-8"
                    )
        except (OSError, shutil.Error) as exc:
            logger.warning("Deleted agent archive failed agent_id=%s: %s", agent.id, exc)


async def teardown_deleted_agents(snapshots: Sequence[AgentTeardown]) -> None:
    await drain_on_cancel(_teardown_deleted_agents(snapshots))


def schedule_agent_teardown(snapshots: Sequence[AgentTeardown]) -> None:
    if snapshots:
        schedule_background_task(teardown_deleted_agents(snapshots), description="teardown deleted agents")


async def _stop_and_persist_missing_container(current: AgentRecord, container_id: str) -> None:
    stopped = await agent_manager.stop_container(current)
    if stopped and current.container_id is None:
        _ = await agent_dao.clear_missing_stopped_container(current.id, container_id)


async def stop_containers(agents: Sequence[AgentRecord]) -> None:
    for agent in agents:
        if agent.container_id not in (None, ""):
            try:
                # An OpenClaw cutover holds this lock from stop to publication or rollback.
                async with setup_workspace_lock(agent_manager._agent_dir(agent.id)):
                    memo_drop("agent", agent.id)
                    current = await agent_dao.get(agent.id)
                    if current is None or current.status != "stopped" or current.container_id in (None, ""):
                        continue
                    await _stop_and_persist_missing_container(current, current.container_id)
            except (ClientNotFoundError, DockerException, GuestOwnershipError, OSError) as exc:
                logger.warning("Agent container stop failed agent_id=%s: %s", agent.id, exc)


def schedule_container_stops(agents: Sequence[AgentRecord]) -> None:
    if agents:
        schedule_background_task(stop_containers(agents), description="stop disabled agent containers")


async def reconcile_stopped_containers(now: datetime) -> None:
    from app.services.company_transfer_cleanup import reconcile_transfer_cleanup

    await reconcile_transfer_cleanup()
    agents = await agent_dao.prepare_container_stops(now)
    await stop_containers(agents)
    await sweep_orphan_containers(now)
