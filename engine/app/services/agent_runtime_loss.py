"""Record, announce and clear the loss of an agent's guest container.

Every entry point is best effort: a failure is logged and never changes the start or dispatch outcome that
observed the loss.
"""

from datetime import datetime
from pathlib import Path
from uuid import UUID, uuid5

from anyio import to_thread

from app.config import get_settings
from app.core.logging import logger
from app.core.notification_kinds import NotificationKind
from app.dao import tenant_dao, user_dao
from app.dao.agent_runtime_loss_dao import agent_runtime_loss_dao
from app.dao.agent_start_dao import agent_start_dao
from app.dao.openclaw_update_dao import openclaw_update_notice_dao
from app.db.session import connection_ctx
from app.records.agent import AgentRecord
from app.records.agent_runtime_loss import AgentRuntimeLoss, RuntimeLossReason
from app.services.agent_start_store import committed_start_connection
from app.services.notification_service import send_notification
from app.services.openclaw_release_backoff import release_backoff
from app.services.openclaw_routing import wake_agent_lanes
from app.services.openclaw_update_snapshot import drop_snapshot

_REASONS: dict[RuntimeLossReason, str] = {
    "container_missing": "The agent's guest container no longer exists on the Docker daemon.",
    "image_unavailable": "The guest image the agent is pinned to no longer exists on the Docker daemon.",
}
_RECOVERY = "The agent manager can start a new container from the agent's settings; pending messages are kept and will be answered."
_SNAPSHOT_NAMESPACE = UUID("6f1d3c2e-8a54-4f0b-9b7e-2d61c0a4e5f7")


def recovery_snapshot_id(agent_id: UUID) -> UUID:
    """Deterministic snapshot id of a start-new-container attempt, distinct from every upgrade run id."""
    return uuid5(_SNAPSHOT_NAMESPACE, f"lost-guest:{agent_id}")


async def report_runtime_loss(agent: AgentRecord, reason: RuntimeLossReason, *, image: str | None = None) -> None:
    """Record the loss and alert every active platform admin once per loss. Never raises."""
    try:
        async with committed_start_connection() as db:
            loss = await agent_runtime_loss_dao.record(
                db, agent.id, reason, recorded_container_id=agent.container_id, image=image
            )
            claimed = await agent_runtime_loss_dao.claim_notification(db, agent.id)
            pending = await agent_runtime_loss_dao.pending_messages(db, agent.id) if claimed else 0
        if claimed:
            # Re-reports arrive on every dispatch or start retry; log the loss once, when it is announced.
            logger.warning("[GuestLoss] agent={} reason={} image={}", agent.id, reason, image)
            await _notify_admins(agent, loss, pending)
    except Exception as exc:
        logger.bind(exception_type=type(exc).__name__).warning("[GuestLoss] recording failed agent={}", agent.id)


async def report_missing_guest(agent: AgentRecord) -> None:
    """Dispatch found no owned guest: report only a running agent with no pending start or active upgrade."""
    if agent.status != "running":
        return
    try:
        async with committed_start_connection() as db:
            operation = await agent_start_dao.get(db, agent.id)
            active_upgrade, _required = await openclaw_update_notice_dao.gate_state(agent.id)
    except Exception as exc:
        logger.bind(exception_type=type(exc).__name__).warning("[GuestLoss] check failed agent={}", agent.id)
        return
    if active_upgrade or (operation is not None and operation.state == "pending"):
        return
    await report_runtime_loss(agent, "container_missing")


async def get_runtime_loss(agent_id: UUID) -> AgentRuntimeLoss | None:
    async with connection_ctx() as db:
        return await agent_runtime_loss_dao.get(db, agent_id)


async def clear_runtime_loss(agent_id: UUID) -> None:
    async with committed_start_connection() as db:
        await agent_runtime_loss_dao.clear(db, agent_id)


async def guest_published(agent: AgentRecord, agent_dir: Path) -> None:
    """After a start publishes a guest: clear the loss, drop leftovers, dispatch held messages now.

    Called inside the exclusive per-agent workspace lock, so a recovery snapshot found here belongs to an
    attempt that is no longer running (an active attempt holds the same lock until it drops or restores it).
    Other processes keep their own release backoff and catch up through the dispatch reconciler.
    """
    try:
        await clear_runtime_loss(agent.id)
    except Exception as exc:
        logger.bind(exception_type=type(exc).__name__).warning("[GuestLoss] clear failed agent={}", agent.id)
    try:
        await to_thread.run_sync(drop_snapshot, agent_dir, recovery_snapshot_id(agent.id))
    except OSError as exc:
        logger.bind(exception_type=type(exc).__name__).warning("[GuestLoss] snapshot drop failed agent={}", agent.id)
    release_backoff.forget_agent(agent.id)
    wake_agent_lanes(agent)


async def _notify_admins(agent: AgentRecord, loss: AgentRuntimeLoss, pending: int) -> None:
    company = "none"
    if agent.tenant_id is not None:
        company = str(agent.tenant_id)
        try:
            tenant = await tenant_dao.get(agent.tenant_id)
            if tenant is not None:
                company = f"{tenant.name} ({agent.tenant_id})"
        except Exception as exc:
            logger.bind(exception_type=type(exc).__name__).warning("[GuestLoss] company lookup failed")
    body = _body(agent, loss, company, pending)
    title = f"Agent {agent.name} lost its guest container"[:200]
    link = f"/companies/{agent.tenant_id}" if agent.tenant_id is not None else None
    recipients = await user_dao.list_active_platform_admins()
    for recipient in recipients:
        try:
            async with committed_start_connection():
                _ = await send_notification(
                    recipient.id,
                    notification_kind=NotificationKind.SYSTEM_ALERT,
                    title=title,
                    body=body,
                    link=link,
                    ref_id=agent.id,
                    sender_name="System",
                )
        except Exception as exc:
            logger.bind(exception_type=type(exc).__name__).warning("[GuestLoss] admin notification failed")


def _body(agent: AgentRecord, loss: AgentRuntimeLoss, company: str, pending: int) -> str:
    detected: datetime = loss.detected_at
    return "\n".join(
        (
            f"Agent: {agent.name} ({agent.id})",
            f"Company: {company}",
            f"Reason: {_REASONS[loss.reason]}",
            f"Recorded container: {loss.recorded_container_id or 'none'}",
            f"Unavailable image: {loss.image or 'not applicable'}",
            f"Pinned image: {agent.openclaw_image_id or 'none'}",
            f"Configured image: {get_settings().MARACLAW_OPENCLAW_IMAGE}",
            f"Detected at: {detected.isoformat()}",
            f"Pending messages: {pending}",
            "",
            _RECOVERY,
        )
    )
