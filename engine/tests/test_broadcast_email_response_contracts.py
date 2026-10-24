from collections.abc import AsyncIterator
from contextlib import nullcontext
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from pydantic import TypeAdapter

from app.api import notification as api
from app.core.security import get_current_user
from app.records.agent import AgentRecord
from app.records.user import UserRecord


@pytest.fixture
def member() -> UserRecord:
    return UserRecord(id=uuid4(), identity_id=None, tenant_id=uuid4(), display_name="Member")


@pytest_asyncio.fixture
async def client(member: UserRecord) -> AsyncIterator[httpx.AsyncClient]:
    app = FastAPI()
    app.include_router(api.router, prefix="/api")
    app.dependency_overrides[get_current_user] = lambda: member
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as transport:
            yield transport
    finally:
        app.dependency_overrides.clear()


@pytest.mark.parametrize(("role", "has_tenant", "status"), [("member", True, 403), ("org_admin", False, 400)])
async def test_broadcast_enforces_role_and_tenant(
    client: httpx.AsyncClient, member: UserRecord, role: str, has_tenant: bool, status: int
) -> None:
    # Given: an unauthorized membership or an administrator without a current tenant.
    member.role = role
    if not has_tenant:
        member.tenant_id = None
    # When: broadcast acceptance is requested.
    response = await client.post("/api/notifications/broadcast", json={"title": "Update"})
    # Then: existing role and tenant checks remain enforced.
    assert response.status_code == status


async def test_broadcast_response_keeps_counts_and_tenant_scope(
    client: httpx.AsyncClient, member: UserRecord, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given: one member and one agent receive an in-app-only broadcast.
    member.role = "org_admin"
    monkeypatch.setattr(api, "connection_ctx", nullcontext)
    users = AsyncMock(return_value=[member])
    agents = AsyncMock(return_value=[AgentRecord(id=uuid4(), name="Agent", creator_id=member.id)])
    sender = AsyncMock()
    monkeypatch.setattr(api.user_dao, "list_active_for_tenant", users)
    monkeypatch.setattr(api.agent_dao, "list_for_tenant", agents)
    monkeypatch.setattr("app.services.notification_service.send_notification", sender)
    # When: the real FastAPI response contract serializes acceptance.
    response = await client.post("/api/notifications/broadcast", json={"title": "Update"})
    # Then: the UUID is additive and existing accepted counts and tenant scope are unchanged.
    assert response.status_code == 200
    receipt = TypeAdapter(dict[str, object]).validate_json(response.content)
    assert TypeAdapter(UUID).validate_python(receipt.pop("broadcast_id"))
    assert receipt == {"ok": True, "users_notified": 1, "agents_notified": 1, "emails_sent": 0}
    users.assert_awaited_once_with(member.tenant_id, exclude_user_id=member.id, include_identity=False)
    agents.assert_awaited_once_with(member.tenant_id)
    assert sender.await_count == 2
