import shutil
from uuid import UUID

from anyio import to_thread
from python_on_whales import ClientNotFoundError, DockerClient
from python_on_whales.exceptions import DockerException, NoSuchContainer

from app.core.logging import logger
from app.dao.agent_deletion_cleanup_dao import agent_deletion_cleanup_dao as cleanup
from app.db.errors import DbError
from app.services.agent_deletion_archive import publish_deletion_archive
from app.services.agent_manager import agent_manager
from app.services.agent_setup_lock import drain_on_cancel, setup_workspace_lock
from app.services.guest_identity import AGENT_LABEL, GuestOwnershipError, require_guest_owner


def _remove_remaining_writers(client: DockerClient, agent_id: UUID) -> None:
    for listed in client.container.list(all=True, filters={"label": f"{AGENT_LABEL}={agent_id}"}):
        try:
            guest = client.container.inspect(listed.id)
            require_guest_owner(agent_id, listed.id, (guest.config.labels or {}).get(AGENT_LABEL))
            client.container.stop(guest.id, time=10)
            type(client.container).remove(client.container, [guest.id])
        except NoSuchContainer:
            continue


async def _complete_cleanup(agent_id: UUID) -> None:
    candidate = await cleanup.get_pending(agent_id)
    if candidate is None:
        return
    async with setup_workspace_lock(candidate.workspace_path, timeout_seconds=0):
        job = await cleanup.get_pending(agent_id)
        if job is None:
            return
        await cleanup.begin_attempt(agent_id)
        try:
            if agent_manager.docker_client is None:
                raise RuntimeError("docker_unavailable")
            if not await agent_manager.remove_container(job.runtime_agent()):
                raise RuntimeError("guest_removal_incomplete")
            await to_thread.run_sync(_remove_remaining_writers, agent_manager.docker_client, agent_id)
            await cleanup.progress(agent_id)
            await to_thread.run_sync(publish_deletion_archive, job)
            await cleanup.progress(agent_id, completed=True)
        except (ClientNotFoundError, DockerException, GuestOwnershipError, RuntimeError, OSError, shutil.Error) as exc:
            await cleanup.progress(agent_id, error=f"{type(exc).__name__}: {exc}")
            logger.opt(exception=exc).warning("Deleted-agent cleanup pending agent_id={}", agent_id)


async def complete_deletion_cleanup(agent_id: UUID) -> None:
    try:
        await drain_on_cancel(_complete_cleanup(agent_id))
    except (DbError, RuntimeError, OSError) as exc:
        logger.opt(exception=exc).warning("Deleted-agent cleanup unavailable agent_id={}", agent_id)


async def reconcile_deletion_cleanup() -> None:
    try:
        pending = await cleanup.pending()
    except (DbError, RuntimeError, OSError) as exc:
        logger.opt(exception=exc).warning("Deleted-agent cleanup queue unavailable")
        return
    for agent_id in pending:
        await complete_deletion_cleanup(agent_id)
