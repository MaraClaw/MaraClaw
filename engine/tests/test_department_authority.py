from dataclasses import replace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.core import access_cache, permissions
from app.records.agent import AgentRecord
from app.records.user import UserRecord


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["role", "tenant", "active"])
async def test_access_uses_identity_read_after_observed_version(monkeypatch, mutation):
    before = UserRecord(id=uuid4(), tenant_id=uuid4(), role="org_admin")
    current = replace(before)
    agent = AgentRecord(
        id=uuid4(), creator_id=uuid4(), name="Shared", tenant_id=before.tenant_id, access_mode="company"
    )

    async def version(_id):
        if mutation == "role":
            current.role = "member"
        elif mutation == "tenant":
            current.tenant_id = uuid4()
        else:
            current.is_active = False
        return "1"

    access_cache.clear_request_memo()
    monkeypatch.setattr(access_cache, "read_acl_version", version)
    monkeypatch.setattr(permissions.user_dao, "get", AsyncMock(return_value=current))
    monkeypatch.setattr(permissions.agent_dao, "get", AsyncMock(return_value=agent))
    monkeypatch.setattr(access_cache, "get_cached_level", AsyncMock(return_value=None))
    fill = AsyncMock()
    monkeypatch.setattr(access_cache, "set_cached_level", fill)
    if mutation == "role":
        assert (await permissions.check_agent_access(before, agent.id))[1] == "use"
        assert fill.await_args is not None
        assert fill.await_args.args[0].role == "member"
        assert fill.await_args.kwargs["observed_ver"] == "1"
    else:
        with pytest.raises(HTTPException) as error:
            await permissions.check_agent_access(before, agent.id)
        assert error.value.status_code == 403
        fill.assert_not_awaited()
