"""Lifecycle hook: cancel an agent's unclaimed Personal bursts inside the transaction that invalidates its route."""

from uuid import UUID

from app.core.json_types import uuid_from_row
from app.dao.personal_burst_dispatch_dao import personal_burst_dispatch_dao
from app.db.session import connection_ctx
from app.records.personal_burst import BurstCancelReason

_OPEN_LANES = (
    "SELECT DISTINCT b.shared_session_id, b.config_id FROM personal_burst_batches b "
    "WHERE b.agent_id = %(agent)s AND b.state IN ('pending', 'sealed') AND b.config_id IN "
    "(SELECT config_id FROM whatsapp_personal_connections WHERE agent_id = %(agent)s) "
    "ORDER BY b.shared_session_id, b.config_id"
)


async def cancel_open_for_agent(agent_id: UUID, reason: BurstCancelReason) -> list[UUID]:
    """Cancel every pending/sealed Personal batch of the agent's own connections, in the caller's transaction.

    Call after the connection row is invalidated: that row lock fences new admissions, so the lane listing is complete.
    Claimed and finished batches are never touched; other connectors, agents and web rows are not selected.
    """
    async with connection_ctx() as db:
        lanes = await db.fetchall(_OPEN_LANES, {"agent": agent_id})
        return [
            gateway_id
            for lane in lanes
            for gateway_id in await personal_burst_dispatch_dao.cancel_open(
                agent_id, uuid_from_row(lane["shared_session_id"]), reason, config_id=uuid_from_row(lane["config_id"])
            )
        ]
