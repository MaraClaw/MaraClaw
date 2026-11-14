"""Company-wide account and agent caps: policy, gate wiring and the HTTP contract (no database)."""

import json
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from fastapi import BackgroundTasks, Request

from app import main
from app.api import auth
from app.core.domain.users import UserRole
from app.db.connection import DbConnection
from app.records.identity import IdentityRecord
from app.records.organization_plan import OrganizationPlanId
from app.records.tenant import TenantRecord
from app.records.user import UserRecord
from app.schemas.schemas import UserLogin, VerifyEmailRequest
from app.services import (
    admin_provisioning as provisioning,
    company_capacity,
    org_membership,
    quota_guard,
    tenant_lifecycle,
    tenant_provisioning,
)
from app.services.auth_audience import AuthAudience
from app.services.company_capacity import (
    CompanyAccountLimitReachedError,
    CompanyAgentLimitReachedError,
    company_caps,
    company_usage,
    free_account_slots,
    require_account_slot,
    require_agent_slots,
)
from app.services.email_verification_service import email_verification_service
from app.services.system_email_service import SystemEmailConfig
from capacity_fixtures import grant_extras

ACCOUNT_MESSAGE = (
    "Your company has used all {limit} accounts. "
    "An org admin can buy extra accounts on the Users page in the admin console."
)
AGENT_MESSAGE = "Your company has used all {limit} agents. An org admin can buy extra agents on the Users page in the admin console."


class _Conn:
    """Answers the statements the gates issue (the ledger read included) and records them beside the tenant lock."""

    def __init__(self, *, is_system: object = False, accounts: int = 0, agents: int = 0) -> None:
        self.is_system: object = is_system
        self.accounts: int = accounts
        self.agents: int = agents
        self.events: list[tuple[str, dict[str, object]]] = []

    async def fetchval(self, query: str, params: dict[str, object] | None = None, *, column: int | str = 0) -> object:
        del column
        sql = " ".join(query.split())
        self.events.append((sql, dict(params or {})))
        if sql.startswith("SELECT is_system FROM tenants"):
            return self.is_system
        if "FROM users" in sql:
            return self.accounts
        if "FROM agents" in sql:
            return self.agents
        raise AssertionError(sql)

    async def fetchone(self, query: str, params: dict[str, object] | None = None) -> dict[str, object] | None:
        """The ledger read of ``effective_extras``: no purchased extras unless a test grants them."""
        sql = " ".join(query.split())
        self.events.append((sql, dict(params or {})))
        if "FROM capacity_orders" in sql:
            return {"accounts": 0, "agents": 0}
        raise AssertionError(sql)

    @property
    def db(self) -> DbConnection:
        return cast("DbConnection", self)

    def statements(self, fragment: str) -> list[tuple[str, dict[str, object]]]:
        return [event for event in self.events if fragment in event[0]]


@pytest.fixture
def tier(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """The tier lookup (a DAO read in production); each test picks the tier."""
    lookup = AsyncMock(return_value=OrganizationPlanId.PRO)
    monkeypatch.setattr(company_capacity, "tier_for_tenant", lookup)
    return lookup


@pytest.fixture
def locks(monkeypatch: pytest.MonkeyPatch) -> list[tuple[UUID, ...]]:
    """Record tenant locks, and a ``LOCK`` event in ``_Conn.events`` so tests can assert lock-before-count order."""
    taken: list[tuple[UUID, ...]] = []

    async def lock_tenants(db: DbConnection, ids: object, *, shared: bool = False) -> None:
        del shared
        ids = tuple(cast("tuple[UUID, ...]", ids))
        taken.append(ids)
        cast("_Conn", db).events.append(("LOCK", {"ids": ids}))

    monkeypatch.setattr(company_capacity, "lock_tenants", lock_tenants)
    return taken


# ── caps ────────────────────────────────────────────────────────────────


async def test_system_organization_is_uncapped_without_reading_the_tier(tier: AsyncMock):
    conn = _Conn(is_system=True)

    assert await company_caps(conn.db, uuid4()) is None
    tier.assert_not_awaited()


async def test_untiered_organization_is_uncapped(tier: AsyncMock):
    tier.return_value = None

    assert await company_caps(_Conn().db, uuid4()) is None


@pytest.mark.parametrize("tenant_id", [None, uuid4()])
async def test_missing_tenant_is_uncapped(tier: AsyncMock, tenant_id: UUID | None):
    conn = _Conn(is_system=None)

    assert await company_caps(conn.db, tenant_id) is None
    tier.assert_not_awaited()


@pytest.mark.parametrize(
    ("plan", "accounts", "agents"),
    [(OrganizationPlanId.PRO, 5, 2), (OrganizationPlanId.MAX, 15, 5), (OrganizationPlanId.ULTRA, 40, 10)],
)
async def test_caps_are_the_included_allocation_plus_the_effective_extras(
    monkeypatch: pytest.MonkeyPatch, tier: AsyncMock, plan: OrganizationPlanId, accounts: int, agents: int
):
    tier.return_value = plan
    tenant_id = uuid4()

    grant_extras(monkeypatch)
    plain = await company_caps(_Conn().db, tenant_id)
    grant_extras(monkeypatch, accounts=3, agents=4)
    raised = await company_caps(_Conn().db, tenant_id)

    assert plain is not None
    assert (plain.tier, plain.account_limit, plain.agent_limit) == (plan, accounts, agents)
    assert raised is not None
    assert (raised.account_limit, raised.agent_limit) == (accounts + 3, agents + 4)
    assert (raised.extras.accounts, raised.extras.agents) == (3, 4)


async def test_usage_counts_active_non_platform_accounts_and_ordinary_unexpired_agents():
    conn = _Conn(accounts=7, agents=3)
    tenant_id = uuid4()

    assert await company_usage(conn.db, tenant_id) == (7, 3)

    ((accounts_sql, accounts_params),) = conn.statements("FROM users")
    assert "tenant_id = %(tenant_id)s" in accounts_sql
    assert "is_active IS TRUE" in accounts_sql
    assert "role <> %(platform_admin)s" in accounts_sql
    assert accounts_params == {"tenant_id": tenant_id, "platform_admin": "platform_admin", "excluded": None}
    ((agents_sql, agents_params),) = conn.statements("FROM agents")
    assert "is_expired IS FALSE" in agents_sql
    assert "is_system IS FALSE" in agents_sql
    assert agents_params == {"tenant_id": tenant_id}


# ── account gate ────────────────────────────────────────────────────────


async def test_account_gate_refuses_the_account_after_the_last_free_one_under_the_tenant_lock(
    tier: AsyncMock, locks: list[tuple[UUID, ...]]
):
    tenant_id = uuid4()
    last_free = _Conn(accounts=4)
    await require_account_slot(last_free.db, tenant_id)
    assert locks == [(tenant_id,)]
    assert [event[0] for event in last_free.events][:1] == ["LOCK"]

    full = _Conn(accounts=5)
    with pytest.raises(CompanyAccountLimitReachedError) as refused:
        await require_account_slot(full.db, tenant_id)

    assert full.events[0][0] == "LOCK"
    assert refused.value.status_code == 409
    assert refused.value.detail == {
        "code": "company_account_limit_reached",
        "message": ACCOUNT_MESSAGE.format(limit=5),
        "limit": 5,
        "used": 5,
    }


async def test_purchased_extra_accounts_raise_the_account_cap(
    monkeypatch: pytest.MonkeyPatch, tier: AsyncMock, locks: list[tuple[UUID, ...]]
):
    grant_extras(monkeypatch, accounts=2)
    await require_account_slot(_Conn(accounts=6).db, uuid4())

    with pytest.raises(CompanyAccountLimitReachedError) as refused:
        await require_account_slot(_Conn(accounts=7).db, uuid4())

    assert (refused.value.limit, refused.value.used) == (7, 7)


async def test_account_gate_never_counts_for_uncapped_organizations(
    monkeypatch: pytest.MonkeyPatch, tier: AsyncMock, locks: list[tuple[UUID, ...]]
):
    system = _Conn(is_system=True, accounts=500)
    await require_account_slot(system.db, uuid4())
    tier.return_value = None
    untiered = _Conn(accounts=500)
    await require_account_slot(untiered.db, uuid4())
    await require_account_slot(untiered.db, None)

    assert system.statements("FROM users") == [] == untiered.statements("FROM users")


async def test_reactivation_excludes_the_member_and_advisory_reads_skip_the_lock(
    tier: AsyncMock, locks: list[tuple[UUID, ...]]
):
    tenant_id, member_id = uuid4(), uuid4()
    conn = _Conn(accounts=4)

    await require_account_slot(conn.db, tenant_id, excluded_user_id=member_id, lock=False)

    assert locks == []
    ((_, params),) = conn.statements("FROM users")
    assert params["excluded"] == member_id


@pytest.mark.parametrize(("used", "free"), [(0, 5), (4, 1), (5, 0), (9, 0)])
async def test_free_account_slots_are_the_cap_less_the_active_accounts_and_never_negative(
    tier: AsyncMock, locks: list[tuple[UUID, ...]], used: int, free: int
):
    tenant_id = uuid4()
    conn = _Conn(accounts=used)

    assert await free_account_slots(conn.db, tenant_id) == free

    assert locks == [(tenant_id,)]
    assert conn.events[0][0] == "LOCK"


async def test_free_account_slots_of_an_uncapped_organization_are_none(tier: AsyncMock, locks: list[tuple[UUID, ...]]):
    system = _Conn(is_system=True, accounts=500)
    assert await free_account_slots(system.db, uuid4()) is None
    tier.return_value = None
    untiered = _Conn(accounts=500)
    assert await free_account_slots(untiered.db, uuid4()) is None

    assert system.statements("FROM users") == [] == untiered.statements("FROM users")


# ── agent gate ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("used", "incoming", "refused"),
    [(0, 2, False), (1, 1, False), (1, 2, True), (2, 1, True), (3, 1, True), (3, 0, False), (9, 0, False)],
)
async def test_agent_gate_refuses_only_a_net_increase_past_the_cap(
    tier: AsyncMock, locks: list[tuple[UUID, ...]], used: int, incoming: int, refused: bool
):
    conn = _Conn(agents=used)  # Pro: 2 included agents

    if not refused:
        await require_agent_slots(conn.db, uuid4(), incoming)
        return
    with pytest.raises(CompanyAgentLimitReachedError) as error:
        await require_agent_slots(conn.db, uuid4(), incoming)
    assert (error.value.limit, error.value.used, error.value.incoming) == (2, used, incoming)
    assert error.value.message == AGENT_MESSAGE.format(limit=2)


async def test_purchased_extra_agents_raise_the_agent_cap_but_not_for_uncapped_organizations(
    monkeypatch: pytest.MonkeyPatch, tier: AsyncMock, locks: list[tuple[UUID, ...]]
):
    grant_extras(monkeypatch, agents=3)
    await require_agent_slots(_Conn(agents=4).db, uuid4())
    with pytest.raises(CompanyAgentLimitReachedError) as refused:
        await require_agent_slots(_Conn(agents=5).db, uuid4())
    assert refused.value.limit == 5

    system = _Conn(is_system=True, agents=500)
    await require_agent_slots(system.db, uuid4())
    assert system.statements("FROM agents") == []


# ── quota_guard: the per-account cap still applies, the company cap comes after it ──


def _member() -> UserRecord:
    return UserRecord(id=uuid4(), role=UserRole.MEMBER, tenant_id=uuid4())


def _ctx(conn: _Conn | None):
    @asynccontextmanager
    async def ctx():
        yield None if conn is None else conn.db

    return ctx


async def test_creation_refuses_at_the_company_cap_while_the_per_account_cap_still_has_room(
    monkeypatch: pytest.MonkeyPatch, tier: AsyncMock, locks: list[tuple[UUID, ...]]
):
    user = _member()
    monkeypatch.setattr(quota_guard, "agent_capacity", AsyncMock(return_value=quota_guard.AgentCapacity(1, 3, True)))
    monkeypatch.setattr(quota_guard, "optional_connection_ctx", _ctx(_Conn(agents=2)))

    with pytest.raises(quota_guard.QuotaExceeded) as refused:
        await quota_guard.check_agent_creation_quota(user, user.tenant_id)

    assert refused.value.quota_type == "company_max_agents"
    assert refused.value.message == AGENT_MESSAGE.format(limit=2)


async def test_creation_reports_the_per_account_cap_first_and_skips_the_company_gate(
    monkeypatch: pytest.MonkeyPatch, tier: AsyncMock, locks: list[tuple[UUID, ...]]
):
    user = _member()
    monkeypatch.setattr(quota_guard, "agent_capacity", AsyncMock(return_value=quota_guard.AgentCapacity(2, 2, True)))
    conn = _Conn(agents=2)
    monkeypatch.setattr(quota_guard, "optional_connection_ctx", _ctx(conn))

    with pytest.raises(quota_guard.QuotaExceeded) as refused:
        await quota_guard.check_agent_creation_quota(user, user.tenant_id)

    assert refused.value.quota_type == "max_agents"
    assert refused.value.message == "Agent creation limit reached (2/2)."
    assert conn.events == []


async def test_creation_accepts_room_in_both_caps_and_skips_the_gate_without_a_connection(
    monkeypatch: pytest.MonkeyPatch, tier: AsyncMock, locks: list[tuple[UUID, ...]]
):
    user = _member()
    monkeypatch.setattr(quota_guard, "agent_capacity", AsyncMock(return_value=quota_guard.AgentCapacity(0, 2, True)))
    monkeypatch.setattr(quota_guard, "optional_connection_ctx", _ctx(_Conn(agents=1)))
    await quota_guard.check_agent_creation_quota(user, user.tenant_id)

    monkeypatch.setattr(quota_guard, "optional_connection_ctx", _ctx(None))
    await quota_guard.check_agent_creation_quota(user, user.tenant_id)


# ── membership writers call the gate under their locks, before they write ──


def _tenant() -> TenantRecord:
    return TenantRecord(id=uuid4(), name="Acme", slug="acme")


def _smtp_config() -> SystemEmailConfig:
    return SystemEmailConfig(
        from_address="bot@example.com",
        from_name="MaraClaw",
        smtp_host="smtp.example.com",
        smtp_port=465,
        smtp_username="bot@example.com",
        smtp_password="secret",
        smtp_ssl=True,
        smtp_timeout_seconds=15,
    )


async def test_joining_a_full_company_is_refused_after_the_locks_and_before_any_write(monkeypatch: pytest.MonkeyPatch):
    tenant = _tenant()
    user = UserRecord(id=uuid4(), tenant_id=None, role=UserRole.MEMBER, is_active=True)
    db = SimpleNamespace()
    order: list[str] = []

    @asynccontextmanager
    async def ctx():
        yield db

    async def lock_tenants(*_args: object, **_kwargs: object) -> None:
        order.append("lock_tenants")

    async def lock_user(*_args: object, **_kwargs: object) -> None:
        order.append("lock_user")

    async def gate(conn: object, tenant_id: UUID) -> None:
        assert conn is db
        assert tenant_id == tenant.id
        order.append("gate")
        raise CompanyAccountLimitReachedError(5, 5)

    update = AsyncMock()
    monkeypatch.setattr(org_membership, "connection_ctx", ctx)
    monkeypatch.setattr("app.core.acl_locks.lock_tenants", lock_tenants)
    monkeypatch.setattr("app.core.acl_locks.lock_user", lock_user)
    monkeypatch.setattr(org_membership.user_dao, "get", AsyncMock(return_value=user))
    monkeypatch.setattr(org_membership.tenant_dao, "get", AsyncMock(return_value=tenant))
    monkeypatch.setattr(org_membership.user_dao, "update", update)
    monkeypatch.setattr("app.services.company_capacity.require_account_slot", gate)

    with pytest.raises(CompanyAccountLimitReachedError):
        await org_membership.attach_user_to_org(user, tenant)

    assert order == ["lock_tenants", "lock_user", "gate"]
    update.assert_not_awaited()


def _admin_ctx(db: object):
    @asynccontextmanager
    async def ctx():
        yield db

    return ctx


def _end_user(*, is_active: bool, tenant_id: UUID) -> UserRecord:
    return UserRecord(id=uuid4(), role=UserRole.MEMBER, tenant_id=tenant_id, is_active=is_active)


@pytest.mark.parametrize("activating", [True, False])
async def test_only_activating_an_end_user_takes_an_account(monkeypatch: pytest.MonkeyPatch, activating: bool):
    tenant_id = uuid4()
    actor = UserRecord(id=uuid4(), role=UserRole.ORG_ADMIN, tenant_id=tenant_id)
    target = _end_user(is_active=not activating, tenant_id=tenant_id)
    db = SimpleNamespace()
    gate = AsyncMock()
    update = AsyncMock(return_value=replace(target, is_active=activating))
    monkeypatch.setattr(provisioning, "connection_ctx", _admin_ctx(db))
    monkeypatch.setattr(provisioning, "require_account_slot", gate)
    monkeypatch.setattr(provisioning.user_dao, "update", update)
    monkeypatch.setattr(provisioning.agent_dao, "disable_for_creator", AsyncMock(return_value=[]))
    monkeypatch.setattr(provisioning.agent_trigger_dao, "disable_for_creator", AsyncMock())
    monkeypatch.setattr(provisioning.agent_schedule_dao, "disable_for_creator", AsyncMock())
    monkeypatch.setattr("app.services.agent_stops.schedule_container_stops", lambda _agents: None)
    monkeypatch.setattr(provisioning, "write_admin_audit", AsyncMock())

    _ = await provisioning.set_end_user_active(actor=actor, target=target, is_active=activating)

    if activating:
        gate.assert_awaited_once_with(db, tenant_id, excluded_user_id=target.id)
    else:
        gate.assert_not_awaited()


async def test_a_refused_activation_writes_nothing(monkeypatch: pytest.MonkeyPatch):
    tenant_id = uuid4()
    actor = UserRecord(id=uuid4(), role=UserRole.ORG_ADMIN, tenant_id=tenant_id)
    target = _end_user(is_active=False, tenant_id=tenant_id)
    update, audit = AsyncMock(), AsyncMock()
    monkeypatch.setattr(provisioning, "connection_ctx", _admin_ctx(SimpleNamespace()))
    monkeypatch.setattr(
        provisioning, "require_account_slot", AsyncMock(side_effect=CompanyAccountLimitReachedError(5, 5))
    )
    monkeypatch.setattr(provisioning.user_dao, "update", update)
    monkeypatch.setattr(provisioning, "write_admin_audit", audit)

    with pytest.raises(CompanyAccountLimitReachedError):
        _ = await provisioning.set_end_user_active(actor=actor, target=target, is_active=True)

    update.assert_not_awaited()
    audit.assert_not_awaited()


async def test_creating_an_org_admin_in_a_full_company_is_refused_before_the_identity_exists(
    monkeypatch: pytest.MonkeyPatch,
):
    tenant = _tenant()
    create_identity = AsyncMock()
    monkeypatch.setattr(provisioning.identity_dao, "get_by_email", AsyncMock(return_value=None))
    monkeypatch.setattr(provisioning.tenant_dao, "get", AsyncMock(return_value=tenant))
    monkeypatch.setattr(provisioning, "hash_password_async", AsyncMock(return_value="hashed"))
    monkeypatch.setattr(provisioning.identity_dao, "create_identity", create_identity)
    monkeypatch.setattr(provisioning, "connection_ctx", _admin_ctx(SimpleNamespace()))
    gate = AsyncMock(side_effect=CompanyAccountLimitReachedError(5, 5))
    monkeypatch.setattr(provisioning, "require_account_slot", gate)

    with pytest.raises(CompanyAccountLimitReachedError):
        _ = await provisioning.create_additional_org_admin(
            tenant_id=tenant.id, admin_email="oa@example.com", admin_password="temp-password"
        )

    gate.assert_awaited_once()
    create_identity.assert_not_awaited()


async def test_reactivating_an_org_admin_takes_an_account_but_a_platform_admin_does_not(
    monkeypatch: pytest.MonkeyPatch,
):
    tenant_id = uuid4()
    genesis = UserRecord(id=uuid4(), role=UserRole.ORG_ADMIN, tenant_id=tenant_id, is_genesis=True)
    peer = UserRecord(id=uuid4(), role=UserRole.ORG_ADMIN, tenant_id=tenant_id, is_active=False)
    platform = UserRecord(id=uuid4(), role=UserRole.PLATFORM_ADMIN, tenant_id=None, is_active=False)
    platform_actor = UserRecord(id=uuid4(), role=UserRole.PLATFORM_ADMIN, tenant_id=None, is_genesis=True)
    db = SimpleNamespace()
    gate = AsyncMock()
    monkeypatch.setattr(provisioning, "connection_ctx", _admin_ctx(db))
    monkeypatch.setattr(provisioning, "require_account_slot", gate)
    monkeypatch.setattr(provisioning.user_dao, "update", AsyncMock(side_effect=lambda **kw: kw["db_obj"]))
    monkeypatch.setattr(provisioning, "write_admin_audit", AsyncMock())
    monkeypatch.setattr(provisioning.identity_dao, "update", AsyncMock())

    _ = await provisioning.set_peer_admin_active(actor=genesis, target=peer, is_active=True)
    gate.assert_awaited_once_with(db, tenant_id, excluded_user_id=peer.id)

    gate.reset_mock()
    _ = await provisioning.set_peer_admin_active(actor=platform_actor, target=platform, is_active=True)
    gate.assert_not_awaited()


@pytest.mark.parametrize(
    ("verified", "smtp", "role", "takes_account"),
    [
        (True, True, UserRole.MEMBER, True),
        (False, False, UserRole.MEMBER, True),
        (False, True, UserRole.MEMBER, False),
        (True, True, UserRole.PLATFORM_ADMIN, False),
    ],
)
async def test_registration_takes_an_account_only_for_an_active_non_platform_member(
    monkeypatch: pytest.MonkeyPatch, verified: bool, smtp: bool, role: UserRole, takes_account: bool
):
    from app.records.identity import IdentityRecord
    from app.services import registration_service as registration

    tenant = _tenant()
    identity = IdentityRecord(id=uuid4(), email="ada@example.com", email_verified=verified)
    db = SimpleNamespace()
    order: list[str] = []
    gate = AsyncMock(side_effect=lambda *_a, **_k: order.append("gate"))
    created: list[dict[str, object]] = []

    async def create(*, obj_in: dict[str, object]) -> UserRecord:
        order.append("create")
        created.append(obj_in)
        return UserRecord(id=uuid4())

    async def lock_tenants(*_args: object, **_kwargs: object) -> None:
        order.append("lock")

    service = registration.RegistrationService()
    monkeypatch.setattr(registration, "optional_connection_ctx", _admin_ctx(db))
    monkeypatch.setattr(registration, "lock_tenants", lock_tenants)
    monkeypatch.setattr(registration, "require_account_slot", gate)
    monkeypatch.setattr(registration.tenant_dao, "get", AsyncMock(return_value=tenant))
    monkeypatch.setattr(registration.user_dao, "create", create)
    monkeypatch.setattr(service, "bind_org_member", AsyncMock())
    monkeypatch.setattr(registration.participant_dao, "create_for_user", AsyncMock())
    config = _smtp_config() if smtp else None
    monkeypatch.setattr(registration, "resolve_email_config_async", AsyncMock(return_value=config))

    _ = await service.create_user_with_identity(identity=identity, role=role, tenant_id=tenant.id)

    assert order == (["lock", "gate", "create"] if takes_account else ["lock", "create"])
    if takes_account:
        gate.assert_awaited_once_with(db, tenant.id)


async def test_registration_into_a_full_company_creates_no_user(monkeypatch: pytest.MonkeyPatch):
    from app.records.identity import IdentityRecord
    from app.services import registration_service as registration

    tenant = _tenant()
    create = AsyncMock()
    service = registration.RegistrationService()
    monkeypatch.setattr(registration, "optional_connection_ctx", _admin_ctx(SimpleNamespace()))
    monkeypatch.setattr(registration, "lock_tenants", AsyncMock())
    monkeypatch.setattr(
        registration, "require_account_slot", AsyncMock(side_effect=CompanyAccountLimitReachedError(5, 5))
    )
    monkeypatch.setattr(registration.tenant_dao, "get", AsyncMock(return_value=tenant))
    monkeypatch.setattr(registration.user_dao, "create", create)
    monkeypatch.setattr(registration, "resolve_email_config_async", AsyncMock(return_value=None))

    with pytest.raises(CompanyAccountLimitReachedError):
        _ = await service.create_user_with_identity(
            identity=IdentityRecord(id=uuid4(), email="ada@example.com", email_verified=True), tenant_id=tenant.id
        )

    create.assert_not_awaited()


async def test_invitations_are_refused_up_front_when_the_company_has_no_free_account(monkeypatch: pytest.MonkeyPatch):
    from fastapi import BackgroundTasks

    from app.api import enterprise as enterprise_api
    from app.schemas.schemas import UserInviteRequest

    tenant = _tenant()
    admin = UserRecord(id=uuid4(), role=UserRole.ORG_ADMIN, tenant_id=tenant.id)
    db = SimpleNamespace()
    gate = AsyncMock(side_effect=CompanyAccountLimitReachedError(5, 5))
    create_code = AsyncMock()
    monkeypatch.setattr(enterprise_api, "optional_connection_ctx", _admin_ctx(db))
    monkeypatch.setattr(enterprise_api, "require_account_slot", gate)
    monkeypatch.setattr(enterprise_api.invitation_code_dao, "create", create_code)
    monkeypatch.setattr(enterprise_api.tenant_dao, "get", AsyncMock(return_value=tenant))
    monkeypatch.setattr(enterprise_api, "_ensure_invitation_email_enabled", AsyncMock())
    monkeypatch.setattr(
        enterprise_api.platform_service, "get_public_base_url", AsyncMock(return_value="https://app.example.com")
    )

    with pytest.raises(CompanyAccountLimitReachedError):
        _ = await enterprise_api._create_invitation_codes(
            enterprise_api.InvitationCodeCreate(email="a@example.com"), admin
        )
    gate.assert_awaited_once_with(db, tenant.id, lock=False)

    with pytest.raises(CompanyAccountLimitReachedError):
        _ = await enterprise_api._invite_users(
            Request({"type": "http", "headers": []}),
            UserInviteRequest(emails=["a@example.com"]),
            BackgroundTasks(),
            admin,
        )
    create_code.assert_not_awaited()


# ── email verification activates memberships only where the company has room ──


def _membership(*, tenant_id: UUID | None, is_active: bool = False, role: UserRole = UserRole.MEMBER) -> UserRecord:
    return UserRecord(id=uuid4(), identity_id=uuid4(), tenant_id=tenant_id, role=role, is_active=is_active)


def _wire_verification(
    monkeypatch: pytest.MonkeyPatch, db: object, memberships: list[UserRecord], gate: object
) -> AsyncMock:
    """Point ``activate_verified_memberships`` at ``memberships`` and ``gate``; return the user write it makes."""
    update = AsyncMock()
    monkeypatch.setattr(org_membership, "connection_ctx", _admin_ctx(db))
    monkeypatch.setattr(org_membership.user_dao, "get_by_identity_id", AsyncMock(return_value=memberships))
    monkeypatch.setattr(org_membership.user_dao, "update", update)
    monkeypatch.setattr("app.services.company_capacity.require_account_slot", gate)
    return update


async def test_verification_activates_an_inactive_membership_that_fits_its_company(monkeypatch: pytest.MonkeyPatch):
    member = _membership(tenant_id=uuid4())
    db, gate = SimpleNamespace(), AsyncMock()
    update = _wire_verification(monkeypatch, db, [member], gate)

    await org_membership.activate_verified_memberships(uuid4())

    gate.assert_awaited_once_with(db, member.tenant_id, excluded_user_id=member.id)
    update.assert_awaited_once_with(db_obj=member, obj_in={"is_active": True})


async def test_verification_leaves_a_membership_of_a_full_company_inactive_and_goes_on(
    monkeypatch: pytest.MonkeyPatch,
):
    full, roomy = _membership(tenant_id=uuid4()), _membership(tenant_id=uuid4())

    async def gate(_db: object, tenant_id: UUID, *, excluded_user_id: UUID) -> None:
        del excluded_user_id
        if tenant_id == full.tenant_id:
            raise CompanyAccountLimitReachedError(5, 5)

    update = _wire_verification(monkeypatch, SimpleNamespace(), [full, roomy], gate)

    await org_membership.activate_verified_memberships(uuid4())

    update.assert_awaited_once_with(db_obj=roomy, obj_in={"is_active": True})


async def test_verification_neither_gates_nor_writes_a_membership_that_is_already_active(
    monkeypatch: pytest.MonkeyPatch,
):
    gate = AsyncMock()
    update = _wire_verification(monkeypatch, SimpleNamespace(), [_membership(tenant_id=uuid4(), is_active=True)], gate)

    await org_membership.activate_verified_memberships(uuid4())

    gate.assert_not_awaited()
    update.assert_not_awaited()


@pytest.mark.parametrize(("tenant_id", "role"), [(None, UserRole.MEMBER), (uuid4(), UserRole.PLATFORM_ADMIN)])
async def test_verification_takes_no_account_for_a_tenantless_membership_or_a_platform_admin(
    monkeypatch: pytest.MonkeyPatch, tenant_id: UUID | None, role: UserRole
):
    member = _membership(tenant_id=tenant_id, role=role)
    gate = AsyncMock()
    update = _wire_verification(monkeypatch, SimpleNamespace(), [member], gate)

    await org_membership.activate_verified_memberships(uuid4())

    gate.assert_not_awaited()
    update.assert_awaited_once_with(db_obj=member, obj_in={"is_active": True})


async def test_the_verification_link_activates_memberships_only_through_the_gated_helper(
    monkeypatch: pytest.MonkeyPatch,
):
    identity = IdentityRecord(id=uuid4(), email="ada@example.com", email_verified=False)
    user = UserRecord(id=uuid4(), identity_id=identity.id, tenant_id=uuid4(), is_active=False)
    activate, write = AsyncMock(), AsyncMock()
    monkeypatch.setattr(org_membership, "activate_verified_memberships", activate)
    monkeypatch.setattr(
        email_verification_service,
        "consume_email_verification_token",
        AsyncMock(return_value={"identity_id": identity.id}),
    )
    monkeypatch.setattr(auth, "connection_ctx", _admin_ctx(None))
    monkeypatch.setattr(auth, "flush_request_transaction", AsyncMock())
    monkeypatch.setattr(auth, "create_access_token", AsyncMock(return_value="token"))
    monkeypatch.setattr(auth.identity_dao, "get", AsyncMock(return_value=identity))
    monkeypatch.setattr(auth.identity_dao, "update", AsyncMock(return_value=identity))
    monkeypatch.setattr(auth.user_dao, "update", write)
    monkeypatch.setattr(auth.user_dao, "get_representative_user_for_identity", AsyncMock(return_value=user))
    monkeypatch.setattr("app.services.domain_admission_auto.stage_verified_domain_admission", AsyncMock())

    _ = await auth.verify_email_flow(VerifyEmailRequest(token="verify-token"), AuthAudience.SHARED)

    activate.assert_awaited_once_with(identity.id)
    write.assert_not_awaited()


async def test_login_without_smtp_activates_memberships_only_through_the_gated_helper(monkeypatch: pytest.MonkeyPatch):
    identity = IdentityRecord(id=uuid4(), email="ada@example.com", password_hash="hash", email_verified=False)
    tenant = _tenant()
    user = UserRecord(id=uuid4(), identity_id=identity.id, tenant_id=tenant.id)
    activate, write = AsyncMock(), AsyncMock()
    monkeypatch.setattr(org_membership, "activate_verified_memberships", activate)
    monkeypatch.setattr("app.services.system_email_service.resolve_email_config_async", AsyncMock(return_value=None))
    monkeypatch.setattr(auth, "connection_ctx", _admin_ctx(None))
    monkeypatch.setattr(auth, "verify_password_async", AsyncMock(return_value=True))
    monkeypatch.setattr(auth, "create_access_token", AsyncMock(return_value="token"))
    monkeypatch.setattr(auth.identity_dao, "get_by_login_identifier", AsyncMock(return_value=identity))
    monkeypatch.setattr(auth.identity_dao, "get", AsyncMock(return_value=identity))
    monkeypatch.setattr(auth.identity_dao, "update", AsyncMock(return_value=identity))
    monkeypatch.setattr(auth.user_dao, "update", write)
    monkeypatch.setattr(auth.user_dao, "get_by_identity_id", AsyncMock(return_value=[user]))
    monkeypatch.setattr(auth.tenant_dao, "get", AsyncMock(return_value=tenant))

    _ = await auth.login_flow(
        UserLogin(login_identifier="ada@example.com", password="secret"),
        BackgroundTasks(),
        Request({"type": "http", "headers": []}),
        AuthAudience.SHARED,
    )

    activate.assert_awaited_once_with(identity.id)
    write.assert_not_awaited()


# ── re-enabling a company and repairing its genesis admin take accounts too ──


@pytest.mark.parametrize("slots", [None, 0, 3])
async def test_re_enabling_a_company_restores_only_its_free_account_slots(
    monkeypatch: pytest.MonkeyPatch, slots: int | None
):
    tenant = _tenant()
    db = SimpleNamespace()
    free, reactivate = AsyncMock(return_value=slots), AsyncMock(return_value=3)
    monkeypatch.setattr(tenant_lifecycle, "connection_ctx", _admin_ctx(db))
    monkeypatch.setattr(tenant_lifecycle.tenant_dao, "update", AsyncMock(return_value=tenant))
    monkeypatch.setattr(tenant_lifecycle, "free_account_slots", free)
    monkeypatch.setattr(tenant_lifecycle.user_dao, "reactivate_for_tenant", reactivate)

    _ = await tenant_lifecycle.set_tenant_active(tenant, is_active=True)

    free.assert_awaited_once_with(db, tenant.id)
    reactivate.assert_awaited_once_with(tenant.id, limit=slots)


async def test_repairing_the_genesis_admin_of_a_full_company_is_refused_before_the_identity_exists(
    monkeypatch: pytest.MonkeyPatch,
):
    tenant = _tenant()
    db = SimpleNamespace()
    create_identity = AsyncMock()
    gate = AsyncMock(side_effect=CompanyAccountLimitReachedError(5, 5))
    monkeypatch.setattr(tenant_provisioning.tenant_dao, "get", AsyncMock(return_value=tenant))
    monkeypatch.setattr(tenant_provisioning.user_dao, "genesis_org_admin_for_tenant", AsyncMock(return_value=None))
    monkeypatch.setattr(tenant_provisioning.identity_dao, "get_by_email", AsyncMock(return_value=None))
    monkeypatch.setattr(tenant_provisioning, "hash_password_async", AsyncMock(return_value="hashed"))
    monkeypatch.setattr(tenant_provisioning.identity_dao, "create_identity", create_identity)
    monkeypatch.setattr(tenant_provisioning, "connection_ctx", _admin_ctx(db))
    monkeypatch.setattr(tenant_provisioning, "require_account_slot", gate)

    with pytest.raises(CompanyAccountLimitReachedError):
        _ = await tenant_provisioning.attach_genesis_org_admin(
            tenant_id=tenant.id, admin_email="oa@example.com", admin_password="temp-password"
        )

    gate.assert_awaited_once_with(db, tenant.id)
    create_identity.assert_not_awaited()


# ── HTTP contract ───────────────────────────────────────────────────────


async def test_the_app_answers_a_refused_account_with_the_409_body_the_clients_read():
    exc = CompanyAccountLimitReachedError(15, 15)

    response = await main.company_account_limit(Request({"type": "http", "headers": []}), exc)

    assert response.status_code == 409
    assert json.loads(bytes(response.body)) == {
        "detail": {
            "code": "company_account_limit_reached",
            "message": ACCOUNT_MESSAGE.format(limit=15),
            "limit": 15,
            "used": 15,
        }
    }
