from datetime import datetime, timedelta
from functools import partial
from uuid import UUID

from anyio import to_thread
from pywhales import ClientNotFoundError, Container, DockerClient
from pywhales.exceptions import DockerException, NoSuchContainer

from app.config import get_settings
from app.core.logging import logger
from app.core.row_memo import memo_drop
from app.dao.agent_dao import agent_dao
from app.services.agent_manager import agent_manager
from app.services.agent_setup_lock import setup_workspace_lock
from app.services.guest_identity import AGENT_LABEL, GuestOwnershipError, require_guest_owner
from app.services.openclaw_update_gate import OpenClawUpdateInProgressError, assert_no_active_run

ORPHAN_GRACE = timedelta(minutes=2)


def _inspect_container(client: DockerClient, reference: str) -> Container:
    return client.container.inspect(reference)


async def _remove_orphan(client: DockerClient, container_id: str, agent_id: UUID) -> None:
    await to_thread.run_sync(partial(client.container.stop, container_id, time=10))
    await to_thread.run_sync(partial(type(client.container).remove, client.container, [container_id]))
    logger.info("Removed orphan container container_id=%s agent_id=%s", container_id, agent_id)


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
            config = container.config
            network_settings = container.network_settings
            created = container.created
            label = (config.labels or {}).get(AGENT_LABEL) if config is not None else None
            networks = (network_settings.networks or {}) if network_settings is not None else {}
            if label is None or network not in networks or created is None or now - created < ORPHAN_GRACE:
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
    existing = {agent.id: agent for agent in await agent_dao.get_many(list(candidates.values()))}
    for container_id, agent_id in candidates.items():
        try:
            known = existing.get(agent_id)
            if known is not None:
                if known.container_id in (None, "", container_id):
                    continue
                async with setup_workspace_lock(agent_manager._agent_dir(agent_id)):
                    memo_drop("agent", agent_id)
                    current = await agent_dao.get(agent_id)
                    if current is None or current.container_id in (None, "", container_id):
                        continue
                    await assert_no_active_run(agent_id)
                    candidate = await to_thread.run_sync(_inspect_container, client, container_id)
                    config = candidate.config
                    network_settings = candidate.network_settings
                    created = candidate.created
                    require_guest_owner(
                        agent_id, container_id, (config.labels or {}).get(AGENT_LABEL) if config else None
                    )
                    if (
                        candidate.id != container_id
                        or network_settings is None
                        or network not in (network_settings.networks or {})
                        or created is None
                        or now - created < ORPHAN_GRACE
                    ):
                        continue
                    recorded = await to_thread.run_sync(_inspect_container, client, current.container_id)
                    config = recorded.config
                    require_guest_owner(
                        agent_id, current.container_id, (config.labels or {}).get(AGENT_LABEL) if config else None
                    )
                    if recorded.id == candidate.id:
                        continue
                    await _remove_orphan(client, container_id, agent_id)
            else:
                await _remove_orphan(client, container_id, agent_id)
        except NoSuchContainer, OpenClawUpdateInProgressError:
            continue
        except (ClientNotFoundError, DockerException, GuestOwnershipError, OSError) as exc:
            logger.warning("Orphan container cleanup failed container_id=%s: %s", container_id, exc)
