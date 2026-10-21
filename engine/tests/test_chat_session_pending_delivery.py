from datetime import UTC, datetime
from typing import Literal, assert_never
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient

from app.api import chat_sessions
from app.core.security import get_current_user
from app.dao.chat_dao import chat_message_dao, chat_session_dao
from app.dao.gateway_report_delivery_dao import gateway_report_delivery_dao
from app.db.errors import LockNotAvailableError
from app.db.session import get_connection, transaction
from app.records.user import UserRecord

pytest_plugins = ("test_chat_submission_postgres",)


@pytest.fixture
async def pending_provider_delivery(receipt_db):
    db = receipt_db
    message_id = uuid4()
    async with transaction(db.first):
        await db.first.execute(
            "UPDATE chat_sessions SET source_channel = 'feishu', external_conv_id = 'provider-dm' WHERE id = %(id)s",
            {"id": db.session_id},
        )
        await chat_message_dao.create(
            obj_in={
                "agent_id": db.agent.id,
                "user_id": db.user.id,
                "role": "user",
                "content": "accepted IM input",
                "conversation_id": str(db.session_id),
            }
        )
        await db.first.execute(
            "INSERT INTO gateway_messages (id, agent_id, conversation_id, content, status) "
            "VALUES (%(id)s, %(agent)s, %(session)s, 'accepted IM input', 'pending')",
            {"id": message_id, "agent": db.agent.id, "session": str(db.session_id)},
        )
        intent = await gateway_report_delivery_dao.create_intent(
            message_id=message_id, destination_kind="im", destination_id=db.session_id, tenant_id=None
        )
    assert intent is not None
    return intent


@pytest.mark.parametrize("completed", [False, True], ids=["accepted-pending", "awaiting-provider-delivery"])
async def test_delete_preserves_pending_provider_destination(receipt_db, pending_provider_delivery, completed):
    db = receipt_db
    app = FastAPI()
    app.include_router(chat_sessions.router)
    app.dependency_overrides[get_current_user] = lambda: db.user
    async with transaction(db.first):
        if completed:
            await db.first.execute(
                "UPDATE gateway_messages SET status = 'completed' WHERE id = %(id)s",
                {"id": pending_provider_delivery.message_id},
            )
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.delete(f"/api/agents/{db.agent.id}/sessions/{db.session_id}")
        assert response.status_code == 409
        assert response.json() == {"detail": {"reason": "session_delivery_pending", "session_id": str(db.session_id)}}
        assert await chat_session_dao.get(db.session_id) is not None
        assert len(await chat_message_dao.list_for_session(conversation_id=str(db.session_id), limit=20)) == 1


@pytest.mark.parametrize("outcome", ["acknowledged", "terminal"])
async def test_delete_releases_provider_destination_after_delivery_resolution(
    receipt_db, pending_provider_delivery, outcome: Literal["acknowledged", "terminal"]
):
    db = receipt_db
    now = datetime.now(UTC)
    async with transaction(db.first):
        await db.first.execute(
            "UPDATE gateway_messages SET status = 'completed' WHERE id = %(id)s",
            {"id": pending_provider_delivery.message_id},
        )
        claims = await gateway_report_delivery_dao.claim_batch(limit=1, lease_seconds=60, now=now)
        assert len(claims) == 1
        claim = claims[0]
        match outcome:
            case "acknowledged":
                resolved = await gateway_report_delivery_dao.acknowledge(claim, now=now)
            case "terminal":
                assert claim.claim_token is not None
                resolved = await gateway_report_delivery_dao.finish_terminal(
                    claim.id, claim.claim_token, "source_agent_unavailable", now=now
                )
            case unreachable:
                assert_never(unreachable)
        assert resolved

    app = FastAPI()
    app.include_router(chat_sessions.router)
    app.dependency_overrides[get_current_user] = lambda: db.user
    async with (
        transaction(db.first),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        response = await client.delete(f"/api/agents/{db.agent.id}/sessions/{db.session_id}")
    assert response.status_code == 204


async def test_delete_checks_pending_delivery_while_holding_session_lock(
    receipt_db, pending_provider_delivery, monkeypatch
):
    db = receipt_db
    original = gateway_report_delivery_dao.has_outstanding_session
    checked: list[UUID] = []

    async def check_locked(session_id: UUID) -> bool:
        assert get_connection() is db.first
        with pytest.raises(LockNotAvailableError):
            async with transaction(db.second):
                await db.second.fetchone(
                    "SELECT id FROM chat_sessions WHERE id = %(id)s FOR UPDATE NOWAIT", {"id": session_id}
                )
        checked.append(session_id)
        return await original(session_id)

    monkeypatch.setattr(gateway_report_delivery_dao, "has_outstanding_session", check_locked)
    with pytest.raises(HTTPException) as rejected:
        async with transaction(db.first):
            await chat_sessions.delete_session(db.agent.id, db.session_id, db.user)
    assert rejected.value.status_code == 409
    assert checked == [db.session_id]


async def test_delete_checks_permissions_before_pending_delivery_disclosure(
    receipt_db, pending_provider_delivery, monkeypatch
):
    lookup = AsyncMock(side_effect=AssertionError("Unauthorized callers must not inspect delivery state"))
    monkeypatch.setattr(gateway_report_delivery_dao, "has_outstanding_session", lookup)
    viewer = UserRecord(id=uuid4(), role="member")
    with pytest.raises(HTTPException) as rejected:
        async with transaction(receipt_db.first):
            await chat_sessions.delete_session(receipt_db.agent.id, receipt_db.session_id, viewer)
    assert rejected.value.status_code == 403
    assert rejected.value.detail == "Not authorized"
    lookup.assert_not_awaited()
