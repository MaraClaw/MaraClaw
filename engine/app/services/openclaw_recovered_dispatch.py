import anyio
from fastapi import HTTPException

from app.dao.gateway_message_dao import gateway_message_dao
from app.dao.openclaw_reply_dao import openclaw_reply_dao
from app.db.session import connection_ctx
from app.records.agent import AgentRecord
from app.records.gateway_message import GatewayMessageRecord
from app.services.openclaw_gateway_delivery import RecordedReport, adopt_recovered_report
from app.services.openclaw_recovered_inputs import successful_recovery
from app.services.openclaw_reply_authorization import authorize_reply_binding
from app.services.openclaw_turns import runtime_session_key


async def settle_recovered_report(agent: AgentRecord, claimed: GatewayMessageRecord) -> RecordedReport | None:
    from app.services.agent_manager import agent_manager

    if claimed.reply_binding_id is None or claimed.delivered_at is None:
        return None
    witness = await anyio.to_thread.run_sync(
        successful_recovery, agent_manager._agent_dir(agent.id), runtime_session_key(claimed), claimed.id
    )
    if witness is None:
        return None
    binding = await openclaw_reply_dao.get(claimed.reply_binding_id)
    if binding is None or (
        binding.agent_id != agent.id
        or binding.owner_id != agent.creator_id
        or binding.tenant_id != agent.tenant_id
        or str(binding.session_id) != claimed.conversation_id
        or binding.session_key != runtime_session_key(claimed)
    ):
        return None
    try:
        async with connection_ctx():
            current = await authorize_reply_binding(binding)
            if current is None:
                return None
            locked = await openclaw_reply_dao.lock(binding)
            if locked is None or (
                locked.agent_id,
                locked.session_id,
                locked.owner_id,
                locked.tenant_id,
                locked.user_id,
            ) != (binding.agent_id, binding.session_id, binding.owner_id, binding.tenant_id, binding.user_id):
                return None
            candidate = await gateway_message_dao.get_for_agent(claimed.id, agent.id)
            if candidate is None or (
                candidate.status != "delivered"
                or candidate.delivered_at != claimed.delivered_at
                or candidate.reply_binding_id != binding.id
                or candidate.conversation_id != str(binding.session_id)
            ):
                return None
            assistant = await openclaw_reply_dao.published_reply(binding, witness.event_id)
            if assistant is None:
                return None
            return await adopt_recovered_report(current, claimed, assistant)
    except HTTPException as error:
        if error.status_code in (403, 404, 409):
            return None
        raise
