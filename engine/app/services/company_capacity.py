"""Company-wide account and agent caps for tiered, non-system organizations.

A tiered organization (an approved admission provisioned it) may hold at most its plan's included accounts plus
purchased extra accounts, and its plan's included agents plus purchased extra agents (``capacity_ledger``). System
organizations (MaraClaw, KABERs) and untiered organizations are uncapped, so ``is_system`` is read before the tier.
Existing members and agents are never touched: only a net increase is refused.

An account is an active membership (``users.is_active``) of the company whose role is not ``platform_admin``: members,
agent admins and organization admins alike, as the plan quote counts the administrator. IM channel and Feishu
auto-created users are not a separate case: ``channel_user_service`` refuses to create them in any non-system,
non-default organization, which every tiered organization is, and ``FeishuService.login_or_register`` has no caller.
An agent is counted exactly as the per-account cap counts it (``agent_dao.count_ordinary_for_creator``), company-wide:
non-expired, non-system, any lifecycle status.

Every gate locks the tenant row (a no-op when the caller already holds it) before it counts, so two writers cannot
both take the last slot. ``conn`` must be the connection of the writing transaction: the tier is read through the
DAO session that joins it.
"""

from dataclasses import dataclass
from typing import ClassVar
from uuid import UUID

from app.core.acl_locks import lock_tenants
from app.core.json_types import int_from_row
from app.db.connection import DbConnection
from app.records.organization_plan import OrganizationPlanId
from app.services.capacity_ledger import CapacityExtras, effective_extras, vn_today
from app.services.organization_plans import plan_for, tier_for_tenant

ACCOUNT_LIMIT_CODE = "company_account_limit_reached"

_ACCOUNTS_SQL = (
    "SELECT COUNT(*) FROM users WHERE tenant_id = %(tenant_id)s AND is_active IS TRUE "
    "AND role <> %(platform_admin)s AND id IS DISTINCT FROM %(excluded)s"
)
_AGENTS_SQL = (
    "SELECT COUNT(*) FROM agents WHERE tenant_id = %(tenant_id)s AND is_expired IS FALSE AND is_system IS FALSE"
)


@dataclass(frozen=True, slots=True)
class CompanyCaps:
    tier: OrganizationPlanId
    account_limit: int
    agent_limit: int
    extras: CapacityExtras


class CompanyAccountLimitReachedError(Exception):
    """The company has no free account; ``status_code`` and ``detail`` are the HTTP contract (409)."""

    status_code: ClassVar[int] = 409
    code: ClassVar[str] = ACCOUNT_LIMIT_CODE

    def __init__(self, limit: int, used: int) -> None:
        self.limit: int = limit
        self.used: int = used
        self.message: str = (
            f"Your company has used all {limit} accounts. "
            "An org admin can buy extra accounts on the Users page in the admin console."
        )
        super().__init__(self.message)

    @property
    def detail(self) -> dict[str, str | int]:
        return {"code": self.code, "message": self.message, "limit": self.limit, "used": self.used}


class CompanyAgentLimitReachedError(Exception):
    """The company cannot take ``incoming`` more ordinary agents."""

    def __init__(self, limit: int, used: int, incoming: int) -> None:
        self.limit: int = limit
        self.used: int = used
        self.incoming: int = incoming
        self.message: str = (
            f"Your company has used all {limit} agents. "
            "An org admin can buy extra agents on the Users page in the admin console."
        )
        super().__init__(self.message)


async def company_caps(conn: DbConnection, tenant_id: UUID | None) -> CompanyCaps | None:
    """The caps of a tiered, non-system organization; ``None`` means uncapped (system, untiered or unknown)."""
    if tenant_id is None:
        return None
    is_system = await conn.fetchval("SELECT is_system FROM tenants WHERE id = %(id)s", {"id": tenant_id})
    if is_system is None or bool(is_system):
        return None
    tier = await tier_for_tenant(tenant_id)
    if tier is None:
        return None
    plan = plan_for(tier)
    extras = await effective_extras(conn, tenant_id, vn_today())
    return CompanyCaps(
        tier=tier,
        account_limit=plan.included_accounts + extras.accounts,
        agent_limit=int(plan.included_agents) + extras.agents,
        extras=extras,
    )


async def _count_accounts(conn: DbConnection, tenant_id: UUID, excluded_user_id: UUID | None = None) -> int:
    value = await conn.fetchval(
        _ACCOUNTS_SQL,
        {"tenant_id": tenant_id, "platform_admin": "platform_admin", "excluded": excluded_user_id},
    )
    return int_from_row(value)


async def _count_agents(conn: DbConnection, tenant_id: UUID) -> int:
    return int_from_row(await conn.fetchval(_AGENTS_SQL, {"tenant_id": tenant_id}))


async def company_usage(conn: DbConnection, tenant_id: UUID) -> tuple[int, int]:
    """``(accounts_used, agents_used)`` of a company, whatever its caps."""
    return await _count_accounts(conn, tenant_id), await _count_agents(conn, tenant_id)


async def require_account_slot(
    conn: DbConnection, tenant_id: UUID | None, *, excluded_user_id: UUID | None = None, lock: bool = True
) -> None:
    """Refuse one more active account in a capped company; call inside the writing transaction.

    ``excluded_user_id`` is the membership being (re)activated, so a racing activation of the same user is not
    counted against itself. ``lock=False`` is the advisory read used when no membership is written.
    """
    if tenant_id is None:
        return
    if lock:
        await lock_tenants(conn, (tenant_id,))
    caps = await company_caps(conn, tenant_id)
    if caps is None:
        return
    used = await _count_accounts(conn, tenant_id, excluded_user_id)
    if used >= caps.account_limit:
        raise CompanyAccountLimitReachedError(caps.account_limit, used)


async def free_account_slots(conn: DbConnection, tenant_id: UUID) -> int | None:
    """Accounts a capped company can still activate, ``None`` when it is uncapped; call inside the writing transaction.

    Locks the tenant row before it counts, like ``require_account_slot``. A company already over its cap (extras
    lapsed, accounts added meanwhile) has none, never a negative number.
    """
    await lock_tenants(conn, (tenant_id,))
    caps = await company_caps(conn, tenant_id)
    if caps is None:
        return None
    return max(0, caps.account_limit - await _count_accounts(conn, tenant_id))


async def require_agent_slots(conn: DbConnection, tenant_id: UUID | None, incoming: int = 1) -> None:
    """Refuse ``incoming`` more ordinary agents in a capped company; call inside the writing transaction.

    A batch that adds no counted agent is never refused, so a company already over its cap keeps its agents.
    """
    if tenant_id is None or incoming <= 0:
        return
    await lock_tenants(conn, (tenant_id,))
    caps = await company_caps(conn, tenant_id)
    if caps is None:
        return
    used = await _count_agents(conn, tenant_id)
    if used + incoming > caps.agent_limit:
        raise CompanyAgentLimitReachedError(caps.agent_limit, used, incoming)
