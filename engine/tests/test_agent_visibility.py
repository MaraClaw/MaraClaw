import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.api import agents
from app.core import permissions
from app.records.user import UserRecord
from app.schemas.agent_directory import AgentDirectoryPosition
from app.services.agent_directory_cursor import AgentDirectoryCursor, parse_agent_directory_cursor


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


async def test_list_visible_agents_delegates_nullable_continuation(monkeypatch):
    user = UserRecord(uuid.uuid4(), tenant_id=uuid.uuid4())
    after = AgentDirectoryPosition(None, uuid.uuid4())
    query = AsyncMock(return_value=[])
    monkeypatch.setattr(permissions.agent_dao, "list_visible_for_user", query)

    assert await permissions.list_visible_agents(user, limit=51, after=after) == []

    assert query.await_args is not None
    assert query.await_args.kwargs["after"] == after
    assert query.await_args.kwargs["limit"] == 51


@pytest.mark.parametrize("created_at", [None, datetime(2026, 1, 1, tzinfo=UTC)])
def test_directory_cursor_preserves_nullable_position(created_at):
    user = make_user()
    cursor = AgentDirectoryCursor(user_id=user.id, tenant_id=user.tenant_id, created_at=created_at, id=uuid.uuid4())

    parsed = parse_agent_directory_cursor(cursor.encode(), user.id, user.tenant_id)

    assert parsed == AgentDirectoryPosition(created_at, cursor.id)


@pytest.mark.parametrize("foreign", ["member", "company", "malformed", "tampered"])
async def test_directory_rejects_foreign_or_invalid_cursor_before_read(monkeypatch, foreign):
    user = UserRecord(uuid.uuid4(), tenant_id=uuid.uuid4())
    raw = AgentDirectoryCursor(
        user_id=uuid.uuid4() if foreign == "member" else user.id,
        tenant_id=uuid.uuid4() if foreign == "company" else user.tenant_id,
        created_at=None,
        id=uuid.uuid4(),
    ).encode()
    if foreign == "malformed":
        raw = "not-a-cursor"
    if foreign == "tampered":
        raw = raw[:40] + ("A" if raw[40] != "A" else "B") + raw[41:]
    query = AsyncMock()
    monkeypatch.setattr(agents, "list_visible_agents", query)

    with pytest.raises(HTTPException) as error:
        await agents.list_agents_page(limit=50, cursor=raw, current_user=user)

    assert error.value.status_code == 400
    query.assert_not_awaited()


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
    rel: Any = SimpleNamespace(
        agent_id=source.id,
        target_agent_id=target.id,
        created_by_user_id=creator_id,
        target_agent=target,
    )

    async def cannot_manage(_user_id, _agent):
        return False

    async def get_agent(agent_id):
        assert agent_id == source.id
        return source

    monkeypatch.setattr(permissions, "user_can_manage_agent_id", cannot_manage)
    monkeypatch.setattr(permissions.agent_dao, "get", get_agent)

    status = await permissions.evaluate_agent_relationship_status(
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
    rel: Any = SimpleNamespace(
        agent_id=source.id,
        target_agent_id=target.id,
        created_by_user_id=creator_id,
        target_agent=target,
    )

    async def can_manage(user_id, _agent):
        return user_id == creator_id

    async def get_agent(agent_id):
        assert agent_id == source.id
        return source

    monkeypatch.setattr(permissions, "user_can_manage_agent_id", can_manage)
    monkeypatch.setattr(permissions.agent_dao, "get", get_agent)

    status = await permissions.evaluate_agent_relationship_status(
        rel,
    )

    assert status["access_allowed"] is True
    assert status["access_status"] == "active"


def test_is_agent_expired_handles_missing_attrs_flag_and_past_date():
    assert permissions.is_agent_expired(SimpleNamespace()) is False
    assert permissions.is_agent_expired(SimpleNamespace(is_expired=True)) is True
    past = datetime.now(UTC) - timedelta(hours=1)
    assert permissions.is_agent_expired(SimpleNamespace(is_expired=False, expires_at=past)) is True
    future = datetime.now(UTC) + timedelta(hours=1)
    assert permissions.is_agent_expired(SimpleNamespace(is_expired=False, expires_at=future)) is False


@pytest.mark.asyncio
async def test_check_agent_access_blocks_expired_use_but_allows_manage(monkeypatch):
    from app.core import access_cache

    access_cache.clear_request_memo()
    tenant = uuid.uuid4()
    member = make_user(role="member", tenant_id=tenant)
    creator = make_user(role="member", tenant_id=tenant)
    expired = SimpleNamespace(
        id=uuid.uuid4(),
        creator_id=creator.id,
        tenant_id=tenant,
        access_mode="company",
        company_access_level="use",
        is_expired=True,
        expires_at=None,
        status="ready",
    )
    monkeypatch.setattr(permissions.agent_dao, "get", AsyncMock(return_value=expired))
    member.is_active = creator.is_active = True
    monkeypatch.setattr(
        permissions.user_dao, "get", AsyncMock(side_effect=lambda uid: member if uid == member.id else creator)
    )
    monkeypatch.setattr(permissions.access_cache, "get_cached_level", AsyncMock(return_value=None))
    monkeypatch.setattr(permissions.access_cache, "read_acl_version", AsyncMock(return_value=0))
    monkeypatch.setattr(permissions.access_cache, "set_cached_level", AsyncMock())

    with pytest.raises(HTTPException) as blocked:
        await permissions.check_agent_access(member, expired.id)
    assert blocked.value.status_code == 403
    assert "expired" in str(blocked.value.detail).lower()

    agent, level = await permissions.check_agent_access(creator, expired.id)
    assert agent is expired
    assert level == "manage"
    access_cache.clear_request_memo()
