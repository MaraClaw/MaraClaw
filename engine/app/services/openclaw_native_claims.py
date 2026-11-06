"""Ordinary native claims, serialized by scope and agent before any lane work."""

from uuid import UUID

from app.core.acl_locks import lock_agent, lock_tenants, lock_user
from app.dao.gateway_message_dao import gateway_message_dao
from app.db.session import connection_ctx, get_connection
from app.records.agent import AgentRecord
from app.records.gateway_message import GatewayMessageRecord
from app.services.agent_work_eligibility import load_agent_for_new_work
from app.services.openclaw_guest_config import GuestConfigWriteError, apply_guest_model
from app.services.openclaw_model_selection import resolve_stored_model
from app.services.openclaw_update_exclusion import guest_write_section
from app.services.openclaw_update_gate import assert_work_allowed


class NativeClaimTransactionError(RuntimeError):
    """A native claim must commit independently before the caller can send RPC."""


async def claim_ordinary_turn(agent: AgentRecord, message_id: UUID) -> GatewayMessageRecord | None:
    """Apply the exact stored model and commit a FIFO head under the captured ownership scope."""
    if get_connection() is not None:
        raise NativeClaimTransactionError
    async with guest_write_section(agent.id), connection_ctx() as db:
        # Tenant and owner are shared so concurrent claims for sibling agents proceed; ACL mutators still exclude.
        await lock_tenants(db, (agent.tenant_id,), shared=True)
        await lock_user(db, agent.creator_id, agent.tenant_id, shared=True)
        await lock_agent(db, agent.id, agent.tenant_id)
        await assert_work_allowed(agent.id)
        fresh = await load_agent_for_new_work(agent.id)
        if (fresh.creator_id, fresh.tenant_id) != (agent.creator_id, agent.tenant_id):
            return None
        message = await gateway_message_dao.claim_ordinary_head(message_id, agent.id)
        if message is None:
            return None
        if message.guest_model_ref is not None:
            resolved = await resolve_stored_model(fresh, message.guest_model_ref)
            if resolved is None:
                raise GuestConfigWriteError
            # A failed write rolls the delivery claim back in this transaction.
            apply_guest_model(fresh, resolved.bundle, resolved.model)
        return message
