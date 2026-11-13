"""Start a new guest container for an agent whose guest is gone.

Runs in the same exclusive lifecycle section as an ordinary start, so the start reconciler defers instead of
racing it. Durable state lives on the host agent directory and is rehydrated as usual; the only extra step is
for an agent pinned to an image that no longer exists: its directory is snapshotted, the pin is cleared with a
compare-and-set and the start uses the configured image. A failed start restores the snapshot and the pin.
"""

from pathlib import Path
from uuid import UUID

from anyio import to_thread
from pywhales import ClientNotFoundError, DockerClient
from pywhales.client_config import ParsingError
from pywhales.exceptions import DockerException

from app.config import get_settings
from app.core.logging import logger
from app.core.row_memo import memo_drop
from app.dao.agent_runtime_loss_dao import agent_runtime_loss_dao
from app.records.agent import AgentRecord
from app.services.agent_lifecycle import LifecycleFailure, LifecycleOutcome, LifecycleSuccess, run_start
from app.services.agent_manager import agent_manager
from app.services.agent_runtime_loss import recovery_snapshot_id
from app.services.agent_setup_lock import drain_on_cancel
from app.services.agent_start_store import committed_start_connection, start_store
from app.services.guest_identity import GuestOwnershipError, owned_guest_containers, stop_guest_containers
from app.services.guest_image import is_local_reference
from app.services.openclaw_update_exclusion import locked_fresh_agent
from app.services.openclaw_update_snapshot import commit_snapshot, drop_snapshot, restore_snapshot

_DOCKER_ERRORS = (ClientNotFoundError, DockerException, ParsingError, OSError)
_INSPECTION_FAILED = LifecycleFailure(502, "guest_start_failed", "Guest container could not be inspected")
_IMAGE_UNAVAILABLE = LifecycleFailure(
    409, "guest_image_unavailable", "Neither the agent's image nor the configured guest image is available"
)


def _unavailable(client: DockerClient, image: str) -> bool:
    """Missing and never pulled: a local reference or an image id (registry references are pulled on start)."""
    return not client.image.exists(image) and (image.startswith("sha256:") or is_local_reference(image))


async def start_new_agent_container(agent_id: UUID) -> LifecycleOutcome:
    async def section() -> LifecycleOutcome:
        async with locked_fresh_agent(agent_id) as agent:
            if agent.setup_pending:
                return LifecycleFailure(409, "guest_setup_incomplete", "Agent setup is incomplete")
            client = agent_manager.docker_client
            unpinned: str | None = None
            if client is not None:
                try:
                    guests = await to_thread.run_sync(owned_guest_containers, client, agent)
                except GuestOwnershipError as exc:
                    logger.warning("Guest recovery ownership refused for agent {}: {}", agent_id, exc)
                    return LifecycleFailure(502, "guest_start_failed", "Guest ownership could not be verified")
                except _DOCKER_ERRORS as exc:
                    logger.warning("Guest recovery inspection failed for agent {}: {}", agent_id, type(exc).__name__)
                    return _INSPECTION_FAILED
                if guests:
                    return LifecycleFailure(
                        409, "guest_container_present", "The agent still has a guest container; use Start"
                    )
                try:
                    plan = await _image_plan(client, agent)
                except _DOCKER_ERRORS as exc:
                    logger.warning("Guest recovery image check failed for agent {}: {}", agent_id, type(exc).__name__)
                    return _INSPECTION_FAILED
                if isinstance(plan, LifecycleFailure):
                    return plan
                unpinned = plan
            agent_dir = agent_manager._agent_dir(agent_id)
            if unpinned is not None:
                try:
                    # A leftover from a crashed attempt predates later workspace writes; never restore it.
                    await to_thread.run_sync(drop_snapshot, agent_dir, recovery_snapshot_id(agent_id))
                    _ = await to_thread.run_sync(commit_snapshot, agent_dir, recovery_snapshot_id(agent_id))
                except OSError as exc:
                    logger.warning("Guest recovery snapshot failed for agent {}: {}", agent_id, type(exc).__name__)
                    return LifecycleFailure(502, "guest_start_failed", "Agent state could not be snapshotted")
                if not await _set_pin(agent_id, unpinned, clear=True):
                    await to_thread.run_sync(drop_snapshot, agent_dir, recovery_snapshot_id(agent_id))
                    return LifecycleFailure(409, "guest_start_ineligible", "The agent's image changed; try again")
            succeeded = False
            try:
                pending = await start_store.begin(agent_id)
                if pending is None:
                    return LifecycleFailure(409, "guest_start_ineligible", "Agent is not eligible to start")
                outcome = await run_start(*pending)
                succeeded = isinstance(outcome, LifecycleSuccess)
                return outcome
            finally:
                if unpinned is not None:
                    if succeeded:
                        await to_thread.run_sync(drop_snapshot, agent_dir, recovery_snapshot_id(agent_id))
                    else:
                        await _roll_back(client, agent, agent_dir, unpinned)

    return await drain_on_cancel(section())


async def _image_plan(client: DockerClient, agent: AgentRecord) -> str | LifecycleFailure | None:
    """``None`` to start as usual, the stale pin to clear first, or a refusal when no image can be used."""
    configured = get_settings().MARACLAW_OPENCLAW_IMAGE
    pinned = agent.openclaw_image_id
    if pinned is not None and pinned != configured and await to_thread.run_sync(_unavailable, client, pinned):
        if await to_thread.run_sync(_unavailable, client, configured):
            return _IMAGE_UNAVAILABLE
        return pinned
    if await to_thread.run_sync(_unavailable, client, pinned or configured):
        return _IMAGE_UNAVAILABLE
    return None


async def _set_pin(agent_id: UUID, image: str, *, clear: bool) -> bool:
    async with committed_start_connection() as db:
        if clear:
            changed = await agent_runtime_loss_dao.unpin_image(db, agent_id, image)
        else:
            changed = await agent_runtime_loss_dao.repin_image(db, agent_id, image)
    memo_drop("agent", agent_id)
    return changed


async def _roll_back(client: DockerClient | None, agent: AgentRecord, agent_dir: Path, image: str) -> None:
    """Remove a writer the failed attempt left, then put the snapshot and pin back. Each step is repeatable.

    If the writer cannot be removed, its state stays live and the snapshot is kept for the next published start
    to drop, since restoring a directory under a running guest would split its state.
    """
    snapshot = recovery_snapshot_id(agent.id)
    try:
        if client is not None:
            _ = await to_thread.run_sync(lambda: stop_guest_containers(client, agent, remove=True))
    except (GuestOwnershipError, *_DOCKER_ERRORS) as exc:
        logger.warning("Guest recovery could not remove the failed guest for agent {}: {}", agent.id, exc)
        return
    try:
        _ = await to_thread.run_sync(restore_snapshot, agent_dir, snapshot)
        await to_thread.run_sync(drop_snapshot, agent_dir, snapshot)
    except OSError as exc:
        logger.warning("Guest recovery snapshot restore failed for agent {}: {}", agent.id, type(exc).__name__)
        return
    if not await _set_pin(agent.id, image, clear=False):
        logger.warning("Guest recovery left agent {} unpinned: another pin was set", agent.id)
