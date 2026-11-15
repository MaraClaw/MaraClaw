"""Company tier preview and apply policy with persistence isolated at the DAO boundary."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import date
from unittest.mock import AsyncMock, Mock
from uuid import UUID

import pytest
from fastapi import HTTPException
from pywhales import DockerClient

from app.core.domain.users import UserRole
from app.db.connection import DbConnection
from app.records.organization_plan import OrganizationPlanId
from app.records.tenant import TenantRecord
from app.records.user import UserRecord
from app.schemas.admin_companies import CompanyTierChangeRequest
from app.services import company_capacity, organization_tier_changes as service
from app.services.agent_manager import agent_manager
from app.services.agent_resource_pressure_docker import GuestInspection
from app.services.capacity_ledger import CapacityExtras

PRO, MAX = OrganizationPlanId.PRO, OrganizationPlanId.MAX
TENANT = UUID(int=20)
ACTOR = UserRecord(id=UUID(int=1), role=UserRole.PLATFORM_ADMIN)


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> dict[str, Mock]:
    db = AsyncMock(spec=DbConnection)
    db.fetchval.return_value = False
    db.fetchall.return_value = []

    state = {"connection_open": False}

    @asynccontextmanager
    async def connection() -> AsyncGenerator[DbConnection]:
        assert isinstance(db, DbConnection)
        state["connection_open"] = True
        try:
            yield db
        finally:
            state["connection_open"] = False

    def inspect(_docker, _references, _timeout):
        assert not state["connection_open"]
        return {"guest-5": GuestInspection("guest-5", str(UUID(int=5)), True, 0, 0, False, None, "", 0, 0)}

    tenant = TenantRecord(id=TENANT, name="Acme", slug="acme", tier=MAX)
    mocks = {
        "tenants": AsyncMock(return_value=[tenant]),
        "tiers": AsyncMock(return_value={TENANT: MAX}),
        "update": AsyncMock(),
        "close": AsyncMock(return_value=4),
        "audit": AsyncMock(),
        "lock": AsyncMock(),
        "usage": AsyncMock(return_value=(9, 8)),
        "extras": AsyncMock(return_value=CapacityExtras(1, 2)),
        "coverage": AsyncMock(return_value=CapacityExtras(3, 1)),
        "db": db,
        "inspect": Mock(side_effect=inspect),
    }
    monkeypatch.setattr(service, "connection_ctx", connection)
    monkeypatch.setattr(service, "lock_tenants", mocks["lock"])
    monkeypatch.setattr(service.tenant_dao, "get_many", mocks["tenants"])
    monkeypatch.setattr(service.tenant_dao, "tiers_for", mocks["tiers"])
    monkeypatch.setattr(service.tenant_dao, "update", mocks["update"])
    monkeypatch.setattr(service.episodes, "close_for_tenant", mocks["close"], raising=False)
    monkeypatch.setattr(service, "write_admin_audit", mocks["audit"])
    monkeypatch.setattr(service, "company_usage", mocks["usage"])
    monkeypatch.setattr(service, "coverage", mocks["coverage"])
    monkeypatch.setattr(service, "vn_today", lambda: date(2026, 12, 31))
    monkeypatch.setattr(service, "inspect_guests", mocks["inspect"], raising=False)
    monkeypatch.setattr(agent_manager, "docker_client", DockerClient())
    monkeypatch.setattr(company_capacity, "effective_extras", mocks["extras"])
    return mocks


async def test_preview_projects_target_caps_owner_overage_guests_and_next_renewal(store):
    store["db"].fetchall.side_effect = [
        [
            {"creator_id": UUID(int=3), "display_name": "Owner", "agents_count": 5},
            {"creator_id": UUID(int=4), "display_name": "Exact", "agents_count": 2},
        ],
        [{"id": UUID(int=5), "name": "Ops", "container_id": "guest-5"}],
    ]

    preview = await service.preview_tier_change(ACTOR, TENANT, PRO)

    assert (preview.current_tier, preview.target_tier) == (MAX, PRO)
    assert preview.accounts.model_dump() == {
        "used": 9,
        "included": 5,
        "extras": 1,
        "limit": 6,
        "over_included": 4,
        "over_limit": 3,
    }
    assert preview.agents.model_dump() == {
        "used": 8,
        "included": 2,
        "extras": 2,
        "limit": 4,
        "over_included": 6,
        "over_limit": 4,
    }
    assert preview.per_account_agent_limit == 2
    assert [(row.user_id, row.agents_count, row.limit, row.overage) for row in preview.owners_over_limit] == [
        (UUID(int=3), 5, 2, 3)
    ]
    assert [(guest.agent_id, guest.container_id) for guest in preview.running_guests] == [(UUID(int=5), "guest-5")]
    assert preview.next_capacity_renewal.model_dump() == {
        "billing_month": date(2027, 1, 1),
        "extra_accounts": 3,
        "extra_agents": 1,
        "account_unit_usd": 5,
        "agent_unit_usd": 35,
        "monthly_usd": 50,
    }
    store["update"].assert_not_awaited()
    store["audit"].assert_not_awaited()


@pytest.mark.parametrize("missing_observation", [False, True])
async def test_preview_uses_owned_running_observations_and_excludes_stopped_foreign_or_missing_guests(
    store, missing_observation: bool
):
    store["db"].fetchall.side_effect = [
        [],
        [
            {"id": UUID(int=n), "name": f"Guest {n}", "container_id": f"guest-{n}"}
            for n in range(5, 9 if missing_observation else 8)
        ],
    ]
    running = GuestInspection("guest-5", str(UUID(int=5)), True, 0, 0, False, None, "", 0, 0)
    store["inspect"].side_effect = None
    store["inspect"].return_value = {
        "guest-5": running,
        "guest-6": replace(running, container_id="guest-6", owner=str(UUID(int=6)), running=False),
        "guest-7": replace(running, container_id="guest-7"),
    }

    preview = await service.preview_tier_change(ACTOR, TENANT, PRO)

    assert [(guest.agent_id, guest.container_id) for guest in preview.running_guests] == [(UUID(int=5), "guest-5")]
    assert preview.guest_observation_available is (not missing_observation)


async def test_preview_marks_unavailable_runtime_observations_without_claiming_guests_are_running(store):
    store["db"].fetchall.side_effect = [[], [{"id": UUID(int=5), "name": "Ops", "container_id": "guest-5"}]]
    store["inspect"].side_effect = None
    store["inspect"].return_value = None

    preview = await service.preview_tier_change(ACTOR, TENANT, PRO)

    assert preview.running_guests == []
    assert preview.guest_observation_available is False


async def test_apply_uses_fresh_locked_tier_instead_of_tenant_snapshot(store):
    store["tiers"].return_value = {TENANT: PRO}

    with pytest.raises(HTTPException) as error:
        await service.apply_tier_change(ACTOR, TENANT, CompanyTierChangeRequest(expected_tier=MAX, target_tier=PRO))

    assert error.value.status_code == 409
    assert error.value.detail == {
        "code": "company_tier_changed",
        "message": "Company tier changed; refresh the preview and try again",
    }
    store["update"].assert_not_awaited()
    store["close"].assert_not_awaited()


async def test_apply_updates_through_tenant_dao_closes_episodes_and_records_field_change(store):
    result = await service.apply_tier_change(
        ACTOR, TENANT, CompanyTierChangeRequest(expected_tier=MAX, target_tier=PRO)
    )

    assert result.model_dump() == {
        "tenant_id": TENANT,
        "previous_tier": MAX,
        "tier": PRO,
        "changed": True,
        "closed_pressure_episodes": 4,
    }
    assert store["update"].await_args.kwargs["obj_in"] == {"tier": PRO}
    store["close"].assert_awaited_once_with(TENANT, reason="tier_changed")
    assert store["audit"].await_args.kwargs["changes"] == {"tier": {"before": "max", "after": "pro"}}
    assert store["audit"].await_args.kwargs["actor"] is ACTOR


async def test_same_tier_is_a_noop_and_preserves_open_pressure(store):
    result = await service.apply_tier_change(
        ACTOR, TENANT, CompanyTierChangeRequest(expected_tier=MAX, target_tier=MAX)
    )

    assert (result.changed, result.closed_pressure_episodes) == (False, 0)
    store["update"].assert_not_awaited()
    store["close"].assert_not_awaited()
    store["audit"].assert_not_awaited()


@pytest.mark.parametrize("preview", [False, True])
async def test_system_companies_are_refused_before_preview_or_apply(store, preview: bool):
    store["tenants"].return_value[0].is_system = True
    operation = (
        service.preview_tier_change(ACTOR, TENANT, PRO)
        if preview
        else service.apply_tier_change(ACTOR, TENANT, CompanyTierChangeRequest(expected_tier=MAX, target_tier=PRO))
    )

    with pytest.raises(HTTPException) as error:
        await operation

    assert error.value.status_code == 409
    assert error.value.detail == {
        "code": "system_company_tier_fixed",
        "message": "System organization tiers are managed by bootstrap",
    }
    store["update"].assert_not_awaited()


async def test_missing_company_is_404(store):
    store["tenants"].return_value = []

    with pytest.raises(HTTPException) as error:
        await service.preview_tier_change(ACTOR, TENANT, PRO)

    assert error.value.status_code == 404
