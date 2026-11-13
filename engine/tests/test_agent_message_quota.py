"""Per-agent daily message cap: local-day resolution, charging policy and the admin handlers, with fakes."""

from datetime import UTC, date, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException

from app.api import users as users_api
from app.core.domain.users import UserRole
from app.records.agent import AgentRecord
from app.records.organization_plan import OrganizationPlanId
from app.records.user import UserRecord
from app.services import agent_message_quota as quota
from app.services.agent_message_quota import AgentDailyMessages
from app.services.quota_guard import QuotaExceeded

NOW = datetime(2026, 11, 13, 20, 30, tzinfo=UTC)


@pytest.mark.parametrize("timezone", [None, "", "Mars/Olympus", "../escape", "tzdata.zi"])
def test_unset_or_invalid_timezone_counts_the_utc_day(timezone: str | None):
    day = quota.local_day(timezone, NOW)

    assert (day.timezone, day.usage_date, day.resets_at) == (
        "UTC",
        date(2026, 11, 13),
        datetime(2026, 11, 14, tzinfo=UTC),
    )


@pytest.mark.parametrize(
    ("timezone", "now", "usage_date", "resets_at"),
    [
        # Already tomorrow in Vietnam while UTC is still on the 13th.
        ("Asia/Ho_Chi_Minh", NOW, date(2026, 11, 14), datetime(2026, 11, 14, 17, tzinfo=UTC)),
        ("America/Los_Angeles", NOW, date(2026, 11, 13), datetime(2026, 11, 14, 8, tzinfo=UTC)),
        # The day the clocks go back is 25 hours long, so midnight is one hour later in UTC terms.
        (
            "Europe/Berlin",
            datetime(2026, 10, 24, 22, 30, tzinfo=UTC),
            date(2026, 10, 25),
            datetime(2026, 10, 25, 23, tzinfo=UTC),
        ),
    ],
)
def test_the_day_is_the_local_calendar_date_of_the_admin_timezone(
    timezone: str, now: datetime, usage_date: date, resets_at: datetime
):
    day = quota.local_day(timezone, now)

    assert (day.timezone, day.usage_date, day.resets_at) == (timezone, usage_date, resets_at)
    assert day.resets_at.utcoffset() is not None
    assert day.resets_at.hour == day.resets_at.minute == 0


def _agent(*, tenant: bool = True, system: bool = False) -> AgentRecord:
    return AgentRecord(
        id=uuid4(), name="Ops", creator_id=uuid4(), tenant_id=uuid4() if tenant else None, is_system=system
    )


def _wire(
    monkeypatch: pytest.MonkeyPatch, tier: OrganizationPlanId | None, timezone: str | None, refuse: bool = False
) -> SimpleNamespace:
    calls = SimpleNamespace(
        tier=AsyncMock(return_value=tier),
        genesis=AsyncMock(return_value=SimpleNamespace(timezone=timezone)),
        charge=AsyncMock(return_value=None if refuse else 1),
        used_on=AsyncMock(return_value={}),
    )
    monkeypatch.setattr(quota, "tier_for_tenant", calls.tier)
    monkeypatch.setattr(quota.user_dao, "genesis_org_admin_for_tenant", calls.genesis)
    monkeypatch.setattr(quota.agent_daily_message_usage_dao, "charge", calls.charge)
    monkeypatch.setattr(quota.agent_daily_message_usage_dao, "used_on", calls.used_on)
    return calls


@pytest.mark.parametrize("agent", [_agent(system=True), _agent(tenant=False)], ids=["system", "tenantless"])
async def test_agents_without_a_cap_are_never_charged(monkeypatch: pytest.MonkeyPatch, agent: AgentRecord):
    calls = _wire(monkeypatch, OrganizationPlanId.PRO, None)

    await quota.charge_agent_daily_message(agent)

    calls.tier.assert_not_awaited()
    calls.charge.assert_not_awaited()


async def test_untiered_organization_is_never_charged(monkeypatch: pytest.MonkeyPatch):
    calls = _wire(monkeypatch, None, None)

    await quota.charge_agent_daily_message(_agent())

    calls.charge.assert_not_awaited()
    calls.genesis.assert_not_awaited()


@pytest.mark.parametrize(
    ("tier", "limit"),
    [(OrganizationPlanId.PRO, 100), (OrganizationPlanId.MAX, 200), (OrganizationPlanId.ULTRA, 500)],
)
async def test_tiered_agent_is_charged_against_the_tier_limit(
    monkeypatch: pytest.MonkeyPatch, tier: OrganizationPlanId, limit: int
):
    agent = _agent()
    calls = _wire(monkeypatch, tier, "Asia/Ho_Chi_Minh")

    await quota.charge_agent_daily_message(agent)

    calls.tier.assert_awaited_once_with(agent.tenant_id)
    calls.genesis.assert_awaited_once_with(agent.tenant_id)
    ((agent_id, usage_date, charged_limit), _) = calls.charge.await_args
    assert (agent_id, charged_limit) == (agent.id, limit)
    assert isinstance(usage_date, date)


async def test_refusal_names_the_limit_and_the_reset_timezone(monkeypatch: pytest.MonkeyPatch):
    _ = _wire(monkeypatch, OrganizationPlanId.PRO, "Asia/Ho_Chi_Minh", refuse=True)

    with pytest.raises(QuotaExceeded) as refused:
        await quota.charge_agent_daily_message(_agent())

    assert refused.value.quota_type == "agent_daily_messages"
    assert refused.value.message == (
        "This agent has reached its daily limit of 100 messages. It resets at midnight (Asia/Ho_Chi_Minh)."
    )


async def test_a_genesis_admin_without_a_timezone_resets_at_utc_midnight(monkeypatch: pytest.MonkeyPatch):
    _ = _wire(monkeypatch, OrganizationPlanId.MAX, None, refuse=True)

    with pytest.raises(QuotaExceeded, match=r"limit of 200 messages\. It resets at midnight \(UTC\)\."):
        await quota.charge_agent_daily_message(_agent())


async def test_missing_genesis_admin_falls_back_to_utc(monkeypatch: pytest.MonkeyPatch):
    calls = _wire(monkeypatch, OrganizationPlanId.ULTRA, None)
    calls.genesis.return_value = None

    snapshot = await quota.daily_messages_for_agents(uuid4(), OrganizationPlanId.ULTRA, [_agent()])

    assert {value.timezone for value in snapshot.values()} == {"UTC"}


async def test_usage_snapshot_covers_only_capped_agents_and_defaults_to_zero(monkeypatch: pytest.MonkeyPatch):
    busy, idle, system = _agent(), _agent(), _agent(system=True)
    calls = _wire(monkeypatch, OrganizationPlanId.PRO, "Asia/Ho_Chi_Minh")
    calls.used_on.return_value = {busy.id: 42}

    snapshot = await quota.daily_messages_for_agents(uuid4(), OrganizationPlanId.PRO, [busy, idle, system])

    assert set(snapshot) == {busy.id, idle.id}
    assert [(value.used, value.limit, value.timezone) for value in (snapshot[busy.id], snapshot[idle.id])] == [
        (42, 100, "Asia/Ho_Chi_Minh"),
        (0, 100, "Asia/Ho_Chi_Minh"),
    ]
    assert snapshot[busy.id].resets_at.utcoffset() is not None
    assert set(calls.used_on.await_args.args[0]) == {busy.id, idle.id}


@pytest.mark.parametrize(
    ("tier", "agents"),
    [(None, [_agent()]), (OrganizationPlanId.PRO, []), (OrganizationPlanId.PRO, [_agent(system=True)])],
    ids=["untiered", "no-agents", "only-system"],
)
async def test_usage_snapshot_is_empty_without_a_cap_and_reads_nothing(
    monkeypatch: pytest.MonkeyPatch, tier: OrganizationPlanId | None, agents: list[AgentRecord]
):
    calls = _wire(monkeypatch, tier, None)

    assert await quota.daily_messages_for_agents(uuid4(), tier, agents) == {}

    calls.genesis.assert_not_awaited()
    calls.used_on.assert_not_awaited()


def _admin_user(role: str, tenant_id: UUID | None = None) -> UserRecord:
    return UserRecord(id=uuid4(), tenant_id=tenant_id, display_name="U", role=UserRole(role), created_at=NOW)


async def test_user_detail_reports_daily_messages_of_capped_agents_only(monkeypatch: pytest.MonkeyPatch):
    tenant_id = uuid4()
    org, member = _admin_user("org_admin", tenant_id), _admin_user("member", tenant_id)
    capped = SimpleNamespace(
        id=uuid4(),
        name="Ops",
        status="idle",
        is_system=False,
        is_expired=False,
        role_description="",
        last_active_at=NOW,
    )
    system = SimpleNamespace(
        id=uuid4(),
        name="System",
        status="idle",
        is_system=True,
        is_expired=False,
        role_description="",
        last_active_at=None,
    )
    resets_at = datetime(2026, 11, 15, tzinfo=UTC)
    monkeypatch.setattr(users_api.user_dao, "get_with_identity", AsyncMock(return_value=member))
    agents = [capped, system]
    monkeypatch.setattr(users_api.agent_dao, "list_for_creator", AsyncMock(return_value=agents))
    monkeypatch.setattr(users_api, "tier_for_tenant", AsyncMock(return_value=OrganizationPlanId.PRO))
    daily = AsyncMock(return_value={capped.id: AgentDailyMessages(7, 100, "Asia/Ho_Chi_Minh", resets_at)})
    monkeypatch.setattr(users_api, "daily_messages_for_agents", daily)

    detail = await users_api.get_user_detail(member.id, org)

    daily.assert_awaited_once_with(tenant_id, OrganizationPlanId.PRO, agents)
    assert detail.model_dump(mode="json")["agents"][0]["daily_messages"] == {
        "used": 7,
        "limit": 100,
        "timezone": "Asia/Ho_Chi_Minh",
        "resets_at": "2026-11-15T00:00:00Z",
    }
    assert detail.agents[1].daily_messages is None


async def test_reset_route_returns_the_zeroed_snapshot_for_an_admin_of_the_same_company(
    monkeypatch: pytest.MonkeyPatch,
):
    tenant_id, agent_id = uuid4(), uuid4()
    org, member = _admin_user("org_admin", tenant_id), _admin_user("member", tenant_id)
    reset = AsyncMock(return_value=AgentDailyMessages(0, 200, "UTC", datetime(2026, 11, 14, tzinfo=UTC)))
    monkeypatch.setattr(users_api.user_dao, "get_with_identity", AsyncMock(return_value=member))
    monkeypatch.setattr(users_api, "reset_agent_daily_messages", reset)

    out = await users_api.reset_user_agent_daily_messages(member.id, agent_id, org, "203.0.113.9")

    assert out.model_dump(mode="json") == {
        "used": 0,
        "limit": 200,
        "timezone": "UTC",
        "resets_at": "2026-11-14T00:00:00Z",
    }
    reset.assert_awaited_once_with(actor=org, target=member, agent_id=agent_id, ip_address="203.0.113.9")


async def test_platform_admin_may_reset_in_any_company(monkeypatch: pytest.MonkeyPatch):
    member = _admin_user("member", uuid4())
    reset = AsyncMock(return_value=AgentDailyMessages(0, 100, "UTC", datetime(2026, 11, 14, tzinfo=UTC)))
    monkeypatch.setattr(users_api.user_dao, "get_with_identity", AsyncMock(return_value=member))
    monkeypatch.setattr(users_api, "reset_agent_daily_messages", reset)

    _ = await users_api.reset_user_agent_daily_messages(member.id, uuid4(), _admin_user("platform_admin"), None)

    reset.assert_awaited_once()


@pytest.mark.parametrize(
    ("actor", "target", "status"),
    [
        (_admin_user("member", None), "same", 403),
        (_admin_user("org_admin", uuid4()), "other-company", 403),
        (_admin_user("org_admin", uuid4()), "missing", 404),
    ],
    ids=["member", "other-company-admin", "unknown-user"],
)
async def test_reset_route_refuses_before_touching_any_agent(
    monkeypatch: pytest.MonkeyPatch, actor: UserRecord, target: str, status: int
):
    found = None if target == "missing" else _admin_user("member", uuid4())
    reset = AsyncMock()
    monkeypatch.setattr(users_api.user_dao, "get_with_identity", AsyncMock(return_value=found))
    monkeypatch.setattr(users_api, "reset_agent_daily_messages", reset)

    with pytest.raises(HTTPException) as refused:
        _ = await users_api.reset_user_agent_daily_messages(uuid4(), uuid4(), actor, None)

    assert refused.value.status_code == status
    reset.assert_not_awaited()
