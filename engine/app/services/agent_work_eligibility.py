from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from fastapi import HTTPException

from app.core.row_memo import memo_drop
from app.dao.agent_dao import agent_dao
from app.dao.company_admission_dao import company_admission_dao
from app.dao.company_transfer_cleanup_dao import company_transfer_cleanup_dao
from app.dao.user_dao import user_dao
from app.records.agent import AgentRecord
from app.records.tenant import TenantRecord
from app.records.user import UserRecord
from app.services.quota_guard import AgentExpired

type AgentWorkBlockCode = Literal[
    "expired", "owner_inactive", "identity_inactive", "tenant_inactive", "owner_tenant_changed", "transfer_pending"
]


@dataclass(frozen=True, slots=True)
class AgentWorkPolicyContext:
    """Pre-read eligibility facts at one instant; evaluating them performs no I/O."""

    owner: UserRecord | None
    tenant: TenantRecord | None
    transfer_pending: bool
    now: datetime


def policy_blocking_codes(agent: AgentRecord, context: AgentWorkPolicyContext) -> tuple[AgentWorkBlockCode, ...]:
    """Shared new-work policy over a caller's single-agent or batched snapshot."""
    codes: list[AgentWorkBlockCode] = []
    if agent.is_expired or (agent.expires_at is not None and agent.expires_at <= context.now):
        codes.append("expired")
    owner = context.owner
    if owner is None or not owner.is_active:
        codes.append("owner_inactive")
    if owner is not None:
        if owner.identity_id is not None and (owner.identity is None or not owner.identity.is_active):
            codes.append("identity_inactive")
        if owner.tenant_id != agent.tenant_id:
            codes.append("owner_tenant_changed")
    if agent.tenant_id is not None:
        tenant = context.tenant
        if tenant is None or not tenant.is_active:
            codes.append("tenant_inactive")
    if context.transfer_pending:
        codes.append("transfer_pending")
    return tuple(codes)


async def agent_work_blocking_codes(agent: AgentRecord) -> tuple[AgentWorkBlockCode, ...]:
    now = datetime.now(UTC)
    owner = await user_dao.get_with_identity(agent.creator_id, fresh=True)
    tenant = await company_admission_dao.tenant(agent.tenant_id) if agent.tenant_id is not None else None
    transfer_pending = await company_transfer_cleanup_dao.is_pending(agent.id)
    return policy_blocking_codes(agent, AgentWorkPolicyContext(owner, tenant, transfer_pending, now))


async def load_agent_for_new_work(agent_id: UUID) -> AgentRecord:
    memo_drop("agent", agent_id)
    agent = await agent_dao.get(agent_id)
    if agent is None or agent.setup_pending:
        raise HTTPException(403, "Agent is unavailable")
    codes = await agent_work_blocking_codes(agent)
    if "expired" in codes:
        raise AgentExpired(agent.name)
    if codes:
        raise HTTPException(403, {"code": "agent_work_blocked", "blocking_codes": list(codes)})
    return agent
