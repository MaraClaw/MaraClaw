from datetime import datetime, timedelta
from functools import partial
from uuid import UUID

from anyio import to_thread
from python_on_whales import ClientNotFoundError
from python_on_whales.exceptions import DockerException, NoSuchContainer

from app.config import get_settings
from app.core.logging import logger
from app.dao.agent_dao import agent_dao
from app.services.agent_manager import agent_manager

ORPHAN_GRACE = timedelta(minutes=2)


async def sweep_orphan_containers(now: datetime) -> None:
    client = agent_manager.docker_client
    network = get_settings().DOCKER_NETWORK
    if client is None or not network:
        return
    try:
        containers = await to_thread.run_sync(
            partial(client.container.list, all=True, filters={"label": "maraclaw.agent_id", "network": network})
        )
        candidates: dict[str, UUID] = {}
        for container in containers:
            label = (container.config.labels or {}).get("maraclaw.agent_id")
            networks = container.network_settings.networks or {}
            if label is None or network not in networks or now - container.created < ORPHAN_GRACE:
                continue
            try:
                agent_id = UUID(label)
            except ValueError:
                continue
            candidates[container.id] = agent_id
    except (ClientNotFoundError, DockerException, OSError) as exc:
        logger.warning("Orphan container listing failed: %s", exc)
        return
    if not candidates:
        return
    existing = {agent.id for agent in await agent_dao.get_many(list(candidates.values()))}
    for container_id, agent_id in candidates.items():
        if agent_id in existing:
            continue
        try:
            await to_thread.run_sync(partial(client.container.stop, container_id, time=10))
            await to_thread.run_sync(partial(type(client.container).remove, client.container, [container_id]))
            logger.info("Removed orphan container container_id=%s agent_id=%s", container_id, agent_id)
        except NoSuchContainer:
            continue
        except (ClientNotFoundError, DockerException, OSError) as exc:
            logger.warning("Orphan container cleanup failed container_id=%s: %s", container_id, exc)
