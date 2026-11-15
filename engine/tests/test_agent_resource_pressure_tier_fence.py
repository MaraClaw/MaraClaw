from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.dao.agent_resource_pressure_episode_dao import agent_resource_pressure_episode_dao as dao
from app.db.connection import DbConnection
from app.records.agent import AgentRecord
from app.records.agent_resource_pressure_episode import PressureResource
from app.records.organization_plan import OrganizationPlanId
from app.services import agent_resource_pressure as pressure


@pytest.mark.parametrize("stored_tier", ["max", None])
async def test_stale_or_deleted_tenant_returns_none_without_writing(monkeypatch, stored_tier):
    db = AsyncMock(spec=DbConnection)
    db.fetchone.return_value = None if stored_tier is None else {"tier": stored_tier}

    @asynccontextmanager
    async def session() -> AsyncGenerator[DbConnection]:
        assert isinstance(db, DbConnection)
        yield db

    monkeypatch.setattr(dao, "session", session)

    result = await dao.record_pressure(
        tenant_id=uuid4(),
        agent_id=uuid4(),
        agent_name="Ops",
        tier=OrganizationPlanId.PRO,
        resource=PressureResource.MEMORY,
        limit_value=1000,
        peak_value=950,
        peak_ratio=0.95,
        threshold_ratio=0.9,
        detail={},
    )

    assert result is None
    assert db.fetchone.await_count == 1


@pytest.mark.parametrize("resource", list(PressureResource))
async def test_observe_sample_accepts_a_fenced_stale_sample(monkeypatch, resource):
    agent = AgentRecord(id=uuid4(), name="Ops", creator_id=uuid4(), tenant_id=uuid4())
    monkeypatch.setattr(dao, "close_open", AsyncMock(return_value=0))
    recording = AsyncMock(return_value=None)
    monkeypatch.setattr(dao, "record_pressure", recording)

    result = await pressure.observe_sample(
        agent, OrganizationPlanId.PRO, pressure.PressureSample(resource, 1000, 950, {})
    )

    assert result is None
    recording.assert_awaited_once()
