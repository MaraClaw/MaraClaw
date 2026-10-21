import hashlib
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import httpx
import pytest
from fastapi import FastAPI

import test_shared_gateway_postgres as shared_fixtures
from app.api import gateway
from app.dao.gateway_message_dao import gateway_message_dao
from app.db.session import get_connection, transaction
from app.records.agent import AgentRecord
from app.records.gateway_message import GatewayMessageRecord
from app.schemas.schemas import GatewayPollResponse
from app.services import openclaw_routing, openclaw_turns
from app.services.openclaw_gateway_delivery import claim_poll_batch
from test_chat_submission_postgres import ReceiptDatabase

receipt_database = shared_fixtures.receipt_database


async def test_shared_wake_dispatches_fifo_head_and_retains_uncertain_claim(
    receipt_database: ReceiptDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given a newer wake notification while older shared inputs are pending.
    db = receipt_database
    messages = await shared_fixtures.seed_shared(db)
    dispatched: list[GatewayMessageRecord] = []

    @asynccontextmanager
    async def connection() -> AsyncGenerator[None]:
        async with transaction(db.first):
            yield

    async def uncertain(_agent: AgentRecord, message: GatewayMessageRecord) -> bool:
        assert get_connection() is None
        dispatched.append(message)
        return False

    monkeypatch.setattr(openclaw_routing, "connection_ctx", connection)
    monkeypatch.setattr(openclaw_turns, "dispatch_shared_turn", uncertain)
    # When the newest notification wakes the guest but admission is uncertain.
    await openclaw_routing._run_inbox_wake(db.agent, messages[-1].content, messages[-1].id)
    # Then only the oldest input was dispatched, without holding database locks.
    assert [message.id for message in dispatched] == [messages[0].id]
    async with transaction(db.second):
        assert await claim_poll_batch(db.agent.id) == []
        head = await gateway_message_dao.get_for_agent(messages[0].id, db.agent.id)
    assert head is not None
    assert head.status == "delivered"


async def test_shared_http_poll_report_keeps_key_and_only_completed_context(receipt_database: ReceiptDatabase) -> None:
    # Given the real gateway router and a real PostgreSQL shared queue.
    db = receipt_database
    messages = await shared_fixtures.seed_shared(db)
    key = "shared-runtime-qa-only"
    async with transaction(db.first):
        await db.first.execute(
            "UPDATE agents SET api_key_hash = %(key)s, status = 'running' WHERE id = %(id)s",
            {"key": hashlib.sha256(key.encode()).hexdigest(), "id": db.agent.id},
        )
    app = FastAPI()
    app.include_router(gateway.router, prefix="/api")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://qa.local") as client:
        # When the guest polls, reports, and polls the following turn over HTTP.
        async with transaction(db.first):
            rejected = await client.get("/api/gateway/poll", headers={"X-Api-Key": "wrong"})
            first = await client.get("/api/gateway/poll", headers={"X-Api-Key": key})
        async with transaction(db.first):
            report = await client.post(
                "/api/gateway/report",
                headers={"X-Api-Key": key},
                json={"message_id": str(messages[0].id), "result": "first HTTP answer"},
            )
        async with transaction(db.first):
            second = await client.get("/api/gateway/poll", headers={"X-Api-Key": key})
    # Then authentication fails closed and the next input shares only prior completed context.
    assert rejected.status_code == 401
    assert first.status_code == report.status_code == second.status_code == 200
    first_payload = GatewayPollResponse.model_validate_json(first.content)
    second_payload = GatewayPollResponse.model_validate_json(second.content)
    assert [item.id for item in first_payload.messages] == [messages[0].id]
    assert [item.id for item in second_payload.messages] == [messages[1].id]
    assert first_payload.messages[0].runtime_session_key == second_payload.messages[0].runtime_session_key
    assert first_payload.messages[0].history == []
    assert [(item.role, item.content) for item in second_payload.messages[0].history] == [
        ("user", "0"),
        ("assistant", "first HTTP answer"),
    ]
