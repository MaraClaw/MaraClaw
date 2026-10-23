from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from uuid import UUID

from app.core.logging import logger
from app.dao.agent_dao import agent_dao
from app.db.session import flush_request_transaction
from app.records.agent import AgentRecord
from app.services.agent_manager import agent_manager
from app.services.agent_setup_lock import drain_on_cancel
from app.services.guest_admission import GuestAdmissionError, GuestStartPreflightError
from app.services.guest_identity import GuestOwnershipError
from app.services.openclaw_update_exclusion import locked_fresh_agent


@dataclass(frozen=True, slots=True)
class LifecycleSuccess:
    agent: AgentRecord


@dataclass(frozen=True, slots=True)
class LifecycleFailure:
    status_code: int
    code: str
    message: str


type LifecycleOutcome = LifecycleSuccess | LifecycleFailure
type RuntimePersistence = Callable[[AgentRecord], Awaitable[AgentRecord]]


async def persist_agent_runtime(agent: AgentRecord) -> AgentRecord:
    return await agent_dao.update(
        db_obj=agent,
        obj_in={
            "status": agent.status,
            "container_id": agent.container_id,
            "container_port": agent.container_port,
            "last_active_at": agent.last_active_at,
            "api_key_hash": agent.api_key_hash,
        },
    )


async def start_agent_guest(agent_id: UUID, persist: RuntimePersistence = persist_agent_runtime) -> LifecycleOutcome:
    async def section() -> LifecycleOutcome:
        async with locked_fresh_agent(agent_id) as agent:
            if agent.setup_pending:
                return LifecycleFailure(409, "guest_setup_incomplete", "Agent setup is incomplete")
            try:
                container_id = await agent_manager.start_container(agent)
            except GuestAdmissionError as exc:
                logger.warning("Guest admission refused for agent {}: {}", agent_id, exc.code)
                return LifecycleFailure(409, exc.code, exc.message)
            except GuestStartPreflightError:
                logger.warning("Guest start preparation failed for agent {}", agent_id)
                return LifecycleFailure(502, "guest_start_failed", "Guest start preparation failed")
            except GuestOwnershipError as exc:
                logger.warning("Guest start ownership refused for agent {}: {}", agent_id, exc)
                return LifecycleFailure(502, "guest_start_failed", "Guest ownership could not be verified")
            saved = await persist(agent)
            await flush_request_transaction()
            if container_id is None and agent.status != "idle":
                return LifecycleFailure(502, "guest_start_failed", "Guest could not be started")
            return LifecycleSuccess(saved)

    return await drain_on_cancel(section())


async def stop_agent_guest(agent_id: UUID, persist: RuntimePersistence = persist_agent_runtime) -> LifecycleOutcome:
    async def section() -> LifecycleOutcome:
        async with locked_fresh_agent(agent_id) as agent:
            stopped = await agent_manager.stop_container(agent)
            if not stopped:
                # Retain the ID so the existing stopped-guest worker can retry the Docker stop.
                agent.status = "stopped"
            saved = await persist(agent)
            await flush_request_transaction()
            if not stopped:
                return LifecycleFailure(502, "guest_stop_failed", "Guest stop is pending retry")
            return LifecycleSuccess(saved)

    return await drain_on_cancel(section())
