"""The organization tier is ``tenants.tier``: the row parser and the transactional read."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.dao.tenant_dao import tenant_dao
from app.records.organization_plan import OrganizationPlanId
from app.records.tenant import TenantRecord
from app.services import organization_plans


def _row(**overrides: object) -> dict[str, object]:
    return {"id": uuid4(), "name": "Acme", "slug": "acme", "sso_domain": None, "tier": "max", **overrides}


def test_a_tenant_row_carries_its_tier():
    assert TenantRecord.from_row(_row()).tier is OrganizationPlanId.MAX


@pytest.mark.parametrize("tier", [None, "", "enterprise", 3])
def test_a_tenant_row_without_a_known_tier_is_refused(tier: object):
    with pytest.raises(ValueError):
        _ = TenantRecord.from_row(_row(tier=tier))


def test_a_tenant_row_has_no_tier_to_fall_back_to():
    row = _row()
    del row["tier"]

    with pytest.raises(KeyError):
        _ = TenantRecord.from_row(row)


async def test_an_agent_with_no_organization_has_no_tier_and_costs_no_read(monkeypatch: pytest.MonkeyPatch):
    read = AsyncMock()
    monkeypatch.setattr(organization_plans.tenant_dao, "tiers_for", read)

    assert await organization_plans.tier_for_tenant(None) is None

    read.assert_not_awaited()


async def test_the_tier_of_an_organization_is_read_from_its_tenant_row(monkeypatch: pytest.MonkeyPatch):
    tenant_id = uuid4()
    read = AsyncMock(return_value={tenant_id: OrganizationPlanId.ULTRA})
    monkeypatch.setattr(organization_plans.tenant_dao, "tiers_for", read)

    assert await organization_plans.tier_for_tenant(tenant_id) is OrganizationPlanId.ULTRA

    read.assert_awaited_once_with([tenant_id])


async def test_an_organization_that_does_not_exist_is_an_error_not_a_missing_tier(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(organization_plans.tenant_dao, "tiers_for", AsyncMock(return_value={}))

    with pytest.raises(LookupError):
        _ = await organization_plans.tier_for_tenant(uuid4())


class _Db:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows: list[dict[str, object]] = rows
        self.queries: list[tuple[str, object]] = []

    async def fetchall(self, query: str, params: object = None) -> list[dict[str, object]]:
        self.queries.append((" ".join(query.split()), params))
        return self.rows


def _session_of(monkeypatch: pytest.MonkeyPatch, db: _Db) -> None:
    @asynccontextmanager
    async def session() -> AsyncGenerator[_Db]:
        yield db

    monkeypatch.setattr(tenant_dao, "session", session)


async def test_the_tiers_of_many_organizations_cost_one_query_and_unknown_ids_are_absent(
    monkeypatch: pytest.MonkeyPatch,
):
    first, second, unknown = uuid4(), uuid4(), uuid4()
    db = _Db([{"id": first, "tier": "pro"}, {"id": second, "tier": "ultra"}])
    _session_of(monkeypatch, db)

    tiers = await tenant_dao.tiers_for([first, second, unknown])

    assert tiers == {first: OrganizationPlanId.PRO, second: OrganizationPlanId.ULTRA}
    assert db.queries == [
        ("SELECT id, tier FROM tenants WHERE id = ANY(%(ids)s)", {"ids": [first, second, unknown]}),
    ]


async def test_no_organizations_cost_no_query(monkeypatch: pytest.MonkeyPatch):
    db = _Db([])
    _session_of(monkeypatch, db)

    assert await tenant_dao.tiers_for([]) == {}

    assert db.queries == []
