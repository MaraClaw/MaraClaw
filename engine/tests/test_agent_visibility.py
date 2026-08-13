import uuid
from types import SimpleNamespace

import pytest

from app.core import permissions


def make_user(**overrides):
    values = {
        "id": uuid.uuid4(),
        "role": "member",
        "tenant_id": uuid.uuid4(),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.mark.asyncio
async def test_list_visible_agents_delegates_to_dao(monkeypatch):
    user = make_user()
    expected = [SimpleNamespace(id=uuid.uuid4())]
    calls = []

    async def list_visible_for_user(**kwargs):
        calls.append(kwargs)
        return expected

    monkeypatch.setattr(permissions.agent_dao, "list_visible_for_user", list_visible_for_user)

    result = await permissions.list_visible_agents(user)

    assert result == expected
    assert calls[0]["user_id"] == user.id
    assert calls[0]["tenant_id"] == user.tenant_id
    assert calls[0]["role"] == user.role


@pytest.mark.asyncio
async def test_list_visible_agents_accepts_explicit_tenant(monkeypatch):
    user = make_user(role="platform_admin", tenant_id=None)
    tenant_id = uuid.uuid4()
    calls = []

    async def list_visible_for_user(**kwargs):
        calls.append(kwargs)
        return []

    monkeypatch.setattr(permissions.agent_dao, "list_visible_for_user", list_visible_for_user)

    await permissions.list_visible_agents(user, tenant_id=tenant_id)

    assert calls[0]["tenant_id"] == tenant_id
    assert calls[0]["role"] == "platform_admin"


@pytest.mark.asyncio
async def test_agent_relationship_status_requires_original_creator_to_still_manage_both_agents(monkeypatch):
    tenant_id = uuid.uuid4()
    creator_id = uuid.uuid4()
    source = SimpleNamespace(
        id=uuid.uuid4(),
        name="Source",
        creator_id=uuid.uuid4(),
        access_mode="company",
        status="ready",
        tenant_id=tenant_id,
    )
    target = SimpleNamespace(
        id=uuid.uuid4(),
        name="Target",
        creator_id=uuid.uuid4(),
        access_mode="company",
        status="ready",
        tenant_id=tenant_id,
    )
    rel = SimpleNamespace(
        agent_id=source.id,
        target_agent_id=target.id,
        created_by_user_id=creator_id,
        target_agent=target,
    )

    async def cannot_manage(_db, _user_id, _agent):
        return False

    async def get_agent(agent_id):
        assert agent_id == source.id
        return source

    monkeypatch.setattr(permissions, "user_can_manage_agent_id", cannot_manage)
    monkeypatch.setattr(permissions.agent_dao, "get", get_agent)

    status = await permissions.evaluate_agent_relationship_status(
        None,
        rel,
        current_user_id=uuid.uuid4(),
    )

    assert status["access_allowed"] is False
    assert status["access_status"] == "restricted"
    assert status["access_status_reason"] == "relationship_creator_no_longer_manages_both_agents"


@pytest.mark.asyncio
async def test_agent_relationship_status_active_when_original_creator_still_manages_both_agents(monkeypatch):
    tenant_id = uuid.uuid4()
    creator_id = uuid.uuid4()
    source = SimpleNamespace(
        id=uuid.uuid4(),
        name="Source",
        creator_id=uuid.uuid4(),
        access_mode="custom",
        status="ready",
        tenant_id=tenant_id,
    )
    target = SimpleNamespace(
        id=uuid.uuid4(),
        name="Target",
        creator_id=uuid.uuid4(),
        access_mode="private",
        status="ready",
        tenant_id=tenant_id,
    )
    rel = SimpleNamespace(
        agent_id=source.id,
        target_agent_id=target.id,
        created_by_user_id=creator_id,
        target_agent=target,
    )

    async def can_manage(_db, user_id, _agent):
        return user_id == creator_id

    async def get_agent(agent_id):
        assert agent_id == source.id
        return source

    monkeypatch.setattr(permissions, "user_can_manage_agent_id", can_manage)
    monkeypatch.setattr(permissions.agent_dao, "get", get_agent)

    status = await permissions.evaluate_agent_relationship_status(
        None,
        rel,
    )

    assert status["access_allowed"] is True
    assert status["access_status"] == "active"
