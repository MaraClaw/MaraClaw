from __future__ import annotations

from contextlib import AsyncExitStack
from contextvars import ContextVar
from datetime import UTC, datetime
from functools import partial
from typing import TYPE_CHECKING
from uuid import UUID

from anyio import to_thread
from pywhales import ClientNotFoundError
from pywhales.exceptions import DockerException

from app.config import get_settings
from app.core.domain.agents import AgentStatus
from app.core.logging import logger
from app.records.agent import AgentRecord
from app.services.agent_mcp_sync import restore_existing_guest
from app.services.agent_start_store import start_store
from app.services.agent_workspace_quota import layer_caused_block, remeasure_after_recreation
from app.services.guest_admission import GuestAdmissionError, GuestStartPreflightError, guest_admission
from app.services.guest_identity import guest_resolutions, inspect_guest_container, owned_guest_containers
from app.services.guest_limits import GuestLimits, limits_differ, resolve_guest_limits
from app.services.guest_readiness import guest_ready

if TYPE_CHECKING:
    from pywhales import DockerClient

    from app.services.agent_manager import AgentManager

start_operation_id: ContextVar[UUID | None] = ContextVar("start_operation_id", default=None)
settings = get_settings()


async def _apply_limits(client: DockerClient, agent: AgentRecord, reference: str, limits: GuestLimits) -> None:
    """Bring a stopped guest's limits to the target before it starts; memory and swap must move together."""
    try:
        await to_thread.run_sync(
            partial(
                client.container.update,
                reference,
                memory=limits.memory_bytes,
                memory_swap=limits.memory_bytes,
                cpus=limits.cpus,
                pids_limit=limits.pids_limit,
                cpu_shares=limits.update_cpu_shares,
            )
        )
    except DockerException as exc:
        logger.warning("Guest limits update failed for agent {}", agent.id)
        raise GuestStartPreflightError("Guest limits could not be updated", code="runtime_failed") from exc


async def _await_ready(client: DockerClient, agent: AgentRecord, *, created: bool) -> str:
    """Require bounded application readiness of the agent's recorded guest before publishing ``running``."""
    reference = agent.container_id
    if reference is None:
        raise GuestStartPreflightError("Guest reference is missing")
    if not await guest_ready(client, reference):
        guest_resolutions.drop(agent.id)
        if created:
            # A failed new writer is quiesced; its durable operation retains discovery ownership.
            await to_thread.run_sync(client.container.stop, reference)
        agent.status = AgentStatus.ERROR
        raise GuestStartPreflightError("Guest did not become ready", code="readiness_failed")
    agent.status = AgentStatus.RUNNING
    agent.last_active_at = datetime.now(UTC)
    return reference


async def start_guest(manager: AgentManager, agent: AgentRecord) -> str | None:
    if agent.setup_pending:
        raise ValueError("Agent setup is incomplete")
    client = manager.docker_client
    if client is None:
        agent.status = AgentStatus.IDLE
        agent.last_active_at = datetime.now(UTC)
        return None
    try:
        guests = await to_thread.run_sync(owned_guest_containers, client, agent)
        if len(guests) > 1:
            raise GuestStartPreflightError("Multiple owned guests require operator reconciliation")
        created = not guests
        if guests:
            guest = guests[0]
            inspected = await to_thread.run_sync(inspect_guest_container, client, guest.id)
            if any(mount.destination == "/home/node/.openclaw" for mount in inspected.mounts or []):
                raise GuestStartPreflightError(
                    "Guest state mount masks image skills; recreate the guest through the OpenClaw update workflow",
                    code="guest_recreation_required",
                )
            state = inspected.state
            host_config = inspected.host_config
            if state is None or state.status not in ("running", "exited", "created"):
                raise GuestStartPreflightError("Guest is not in a resumable state")
            resume = state.status in ("exited", "created")
            if (
                resume
                and any(mount.destination == "/home/node/custom-state" for mount in inspected.mounts or [])
                and await layer_caused_block(agent)
            ):
                # Only the container's writable layer keeps the agent over its workspace allowance, and the durable
                # state is on the host mount: a new container on the guest's own image clears the layer.
                reference = await manager.replace_container(
                    agent, image=inspected.image, operation_id=start_operation_id.get()
                )
                if reference is None:
                    return None
                await remeasure_after_recreation(agent)
                return await _await_ready(client, agent, created=True)
            target: GuestLimits | None = None
            if resume:
                if host_config is None or host_config.memory is None:
                    raise GuestAdmissionError("guest_memory_unbounded", "Guest has no hard memory limit")
                target = await resolve_guest_limits(agent)
            await manager._prepare_tavily_proxy_token(agent.id)
            async with AsyncExitStack() as stack:
                if target is not None and host_config is not None:
                    # Admission reserves the target, not the stale inspected limit, and precedes the update,
                    # so a refusal leaves the guest unchanged.
                    await stack.enter_async_context(
                        guest_admission(
                            client, agent, manager._agent_dir(agent.id).parent, memory_limit=target.memory_bytes
                        )
                    )
                    if limits_differ(host_config, target):
                        await _apply_limits(client, agent, guest.id, target)
                await restore_existing_guest(manager, agent, running=not resume)
                agent.container_id = guest.id
                port = next(
                    (
                        int(host)
                        for port, host in guest.port_bindings
                        if port == f"{settings.OPENCLAW_GATEWAY_PORT}/tcp"
                    ),
                    None,
                )
                agent.container_port = port or agent.container_port
                operation = start_operation_id.get()
                if operation is not None and not await start_store.observe(operation, agent):
                    raise GuestStartPreflightError("Guest start was superseded")
                if resume:
                    guest_resolutions.drop(agent.id)
                    await to_thread.run_sync(client.container.start, guest.id)
        else:
            reference = await manager.replace_container(agent, create_only=True, operation_id=start_operation_id.get())
            if reference is None:
                return None
        return await _await_ready(client, agent, created=created)
    except DockerException as exc:
        raise GuestStartPreflightError("Guest start inspection or readiness failed", code="runtime_failed") from exc
    except ClientNotFoundError as exc:
        raise GuestStartPreflightError("Guest start inspection or readiness failed") from exc
