from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import override
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from tests.channel_submission_fixtures import ChannelDatabase, channel_db, receipt_db

from app.core.domain.agents import AgentStatus
from app.core.row_memo import clear_entity_memo
from app.dao.user_dao import user_dao
from app.db.session import transaction
from app.records.agent import AgentRecord
from app.records.identity import IdentityRecord
from app.records.user import UserRecord
from app.services import agent_work_eligibility as eligibility

__all__ = ["channel_db", "receipt_db"]


@pytest.fixture
def principal(monkeypatch):
    owner = UserRecord(id=uuid4())
    monkeypatch.setattr(eligibility.user_dao, "get_with_identity", AsyncMock(return_value=owner))
    monkeypatch.setattr(eligibility.company_transfer_cleanup_dao, "is_pending", AsyncMock(return_value=False))
    return owner


@pytest.mark.parametrize("offset", [-1, 0, 1])
async def test_timestamp_boundary_without_materialized_expiry(principal, monkeypatch, offset):
    now = datetime(2026, 10, 26, tzinfo=UTC)

    class Clock(datetime):
        @classmethod
        @override
        def now(cls, tz=None):
            return now

    monkeypatch.setattr(eligibility, "datetime", Clock)
    agent = AgentRecord(
        id=uuid4(), name="Boundary", creator_id=principal.id, expires_at=now + timedelta(seconds=offset)
    )
    codes = await eligibility.agent_work_blocking_codes(agent)
    assert codes == (("expired",) if offset <= 0 else ())


@pytest.mark.parametrize("owner_state", ["missing", "inactive", "identity_missing", "identity_inactive", "legacy"])
async def test_owner_and_nullable_identity_contract(principal, monkeypatch, owner_state):
    owner = principal
    match owner_state:
        case "missing":
            owner = None
        case "inactive":
            owner = replace(principal, is_active=False)
        case "identity_missing":
            owner = replace(principal, identity_id=uuid4())
        case "identity_inactive":
            identity = IdentityRecord(id=uuid4(), is_active=False)
            owner = replace(principal, identity_id=identity.id, identity=identity)
        case "legacy":
            pass
    lookup = AsyncMock(return_value=owner)
    monkeypatch.setattr(eligibility.user_dao, "get_with_identity", lookup)
    agent = AgentRecord(id=uuid4(), name="Owner", creator_id=principal.id, status=AgentStatus.STOPPED)
    codes = await eligibility.agent_work_blocking_codes(agent)
    expected = {
        "missing": ("owner_inactive",),
        "inactive": ("owner_inactive",),
        "identity_missing": ("identity_inactive",),
        "identity_inactive": ("identity_inactive",),
        "legacy": (),
    }
    assert codes == expected[owner_state]
    lookup.assert_awaited_once_with(principal.id, fresh=True)


@pytest.mark.parametrize("state", ["owner", "identity", "tenant", "moved", "transfer", "expired"])
async def test_policy_reads_committed_postgres_state_despite_cached_principal(channel_db: ChannelDatabase, state):
    fixture = channel_db
    identity_id, target_id, transfer_id = uuid4(), uuid4(), uuid4()
    async with transaction(fixture.db.first):
        await fixture.db.first.execute("INSERT INTO identities(id) VALUES (%(id)s)", {"id": identity_id})
        await fixture.db.first.execute(
            "UPDATE users SET identity_id=%(identity)s WHERE id=%(id)s",
            {"identity": identity_id, "id": fixture.db.user.id},
        )
        clear_entity_memo()
        cached = await user_dao.get_with_identity(fixture.db.user.id)
        assert cached is not None
        assert cached.is_active
        assert cached.identity is not None
        assert cached.identity.is_active
    async with transaction(fixture.db.second):
        match state:
            case "owner":
                await fixture.db.second.execute(
                    "UPDATE users SET is_active=FALSE WHERE id=%(id)s", {"id": fixture.db.user.id}
                )
            case "identity":
                await fixture.db.second.execute(
                    "UPDATE identities SET is_active=FALSE WHERE id=%(id)s", {"id": identity_id}
                )
            case "tenant":
                await fixture.db.second.execute("UPDATE tenants SET is_active=FALSE")
            case "moved" | "transfer":
                await fixture.db.second.execute(
                    "INSERT INTO tenants(id,name,slug) VALUES (%(id)s,'Target','target')", {"id": target_id}
                )
                if state == "moved":
                    await fixture.db.second.execute(
                        "UPDATE users SET tenant_id=%(tenant)s WHERE id=%(id)s",
                        {"tenant": target_id, "id": fixture.db.user.id},
                    )
                else:
                    await fixture.db.second.execute(
                        "INSERT INTO company_transfer_requests(id,applicant_user_id,source_tenant_id,target_tenant_id,"
                        "choice,method,fee_amount_minor,currency,instructions) "
                        "VALUES (%(id)s,%(user)s,%(source)s,%(target)s,'paid','sso',0,'USD','')",
                        {
                            "id": transfer_id,
                            "user": fixture.db.user.id,
                            "source": fixture.tenant_id,
                            "target": target_id,
                        },
                    )
                    await fixture.db.second.execute(
                        "INSERT INTO company_transfer_runtime_cleanup(agent_id,transfer_request_id,choice) "
                        "VALUES (%(agent)s,%(transfer)s,'paid')",
                        {"agent": fixture.db.agent.id, "transfer": transfer_id},
                    )
            case "expired":
                await fixture.db.second.execute(
                    "UPDATE agents SET expires_at=now()-interval '1 second',is_expired=FALSE"
                )
    agent = replace(fixture.db.agent, tenant_id=fixture.tenant_id)
    async with transaction(fixture.db.first):
        if state == "expired":
            with pytest.raises(eligibility.AgentExpired):
                await eligibility.load_agent_for_new_work(agent.id)
            return
        codes = await eligibility.agent_work_blocking_codes(agent)
    expected = {
        "owner": ("owner_inactive",),
        "identity": ("identity_inactive",),
        "tenant": ("tenant_inactive",),
        "moved": ("owner_tenant_changed",),
        "transfer": ("transfer_pending",),
    }
    assert codes == expected[state]
