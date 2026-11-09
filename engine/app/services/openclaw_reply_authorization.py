from app.core.acl_locks import lock_agent, lock_tenants, lock_user
from app.core.permissions import check_agent_access
from app.dao.chat_dao import chat_session_dao
from app.dao.openclaw_reply_dao import openclaw_reply_dao
from app.dao.user_dao import user_dao
from app.db.session import connection_ctx
from app.records.agent import AgentRecord
from app.records.openclaw_reply_binding import OpenClawReplyBinding
from app.services.agent_work_eligibility import load_agent_for_new_work


async def authorize_reply_binding(binding: OpenClawReplyBinding) -> AgentRecord | None:
    async with connection_ctx() as db:
        await lock_tenants(db, (binding.tenant_id,), shared=True)
        for user_id in sorted({binding.owner_id, binding.user_id}):
            await lock_user(db, user_id, binding.tenant_id, shared=True)
        await lock_agent(db, binding.agent_id, binding.tenant_id)
        agent = await load_agent_for_new_work(binding.agent_id)
        if agent.creator_id != binding.owner_id or agent.tenant_id != binding.tenant_id:
            return None
        session = await chat_session_dao.lock_for_agent(binding.session_id, agent.id)
        if session is None or session.user_id != binding.user_id or session.source_channel not in ("web", "shared"):
            return None
        user = await user_dao.get_with_identity(binding.user_id, fresh=True)
        if user is None or not user.is_active or user.must_change_password or user.tenant_id != binding.tenant_id:
            return None
        if user.identity_id is not None and (user.identity is None or not user.identity.is_active):
            return None
        await check_agent_access(user, agent.id)
        return agent if await openclaw_reply_dao.lock(binding) is not None else None
