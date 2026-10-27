from __future__ import annotations

from contextlib import AsyncExitStack
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import UUID

from anyio import to_thread
from pywhales import ClientNotFoundError
from pywhales.exceptions import DockerException

from app.config import get_settings
from app.core.domain.agents import AgentStatus
from app.records.agent import AgentRecord
from app.services.agent_start_store import start_store
from app.services.guest_admission import GuestAdmissionError, GuestStartPreflightError, guest_admission
from app.services.guest_identity import inspect_guest_container, owned_guest_containers
from app.services.guest_readiness import guest_ready

if TYPE_CHECKING:
    from app.services.agent_manager import AgentManager

start_operation_id: ContextVar[UUID | None] = ContextVar("start_operation_id", default=None)
settings = get_settings()


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
            state = inspected.state
            host_config = inspected.host_config
            if state is None or state.status not in ("running", "exited", "created"):
                raise GuestStartPreflightError("Guest is not in a resumable state")
            resume = state.status in ("exited", "created")
            if resume and (host_config is None or host_config.memory is None):
                raise GuestAdmissionError("guest_memory_unbounded", "Guest has no hard memory limit")
            async with AsyncExitStack() as stack:
                if resume:
                    await stack.enter_async_context(
                        guest_admission(
                            client,
                            agent,
                            manager._agent_dir(agent.id).parent,
                            memory_limit=(host_config.memory or 0) if host_config is not None else 0,
                        )
                    )
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
                    await to_thread.run_sync(client.container.start, guest.id)
        else:
            reference = await manager.replace_container(agent, create_only=True, operation_id=start_operation_id.get())
            if reference is None:
                return None
        reference = agent.container_id
        if reference is None:
            raise GuestStartPreflightError("Guest reference is missing")
        if not await guest_ready(client, reference):
            if created:
                # A failed new writer is quiesced; its durable operation retains discovery ownership.
                await to_thread.run_sync(client.container.stop, reference)
            agent.status = AgentStatus.ERROR
            raise GuestStartPreflightError("Guest did not become ready", code="readiness_failed")
        agent.status = AgentStatus.RUNNING
        agent.last_active_at = datetime.now(UTC)
        return reference
    except DockerException as exc:
        raise GuestStartPreflightError("Guest start inspection or readiness failed", code="runtime_failed") from exc
    except ClientNotFoundError as exc:
        raise GuestStartPreflightError("Guest start inspection or readiness failed") from exc
