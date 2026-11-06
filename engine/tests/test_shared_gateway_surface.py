import hashlib
from datetime import UTC, datetime

import httpx
import pytest
from fastapi import FastAPI

import test_shared_gateway_postgres as shared_fixtures
from app.api import gateway
from app.dao.gateway_message_dao import gateway_message_dao
from app.db.session import get_connection, transaction
from app.records.agent import AgentRecord
from app.records.gateway_message import GatewayMessageRecord
from app.records.openclaw_dispatch import SharedDispatchRequest
from app.services import openclaw_gateway_rpc, openclaw_routing, openclaw_turns
from app.services.openclaw_gateway_rpc import UncertainOutcome
from app.services.openclaw_shared_dispatch import claim_ready_shared
from app.services.turn_timing import TurnTimeline
from test_chat_submission_postgres import ReceiptDatabase

receipt_database = shared_fixtures.receipt_database
pooled_database = shared_fixtures.pooled_database


async def test_shared_wake_dispatches_fifo_head_and_retains_uncertain_claim(
    pooled_database: ReceiptDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given a newer wake notification while older shared inputs are pending.
    db = pooled_database
    messages = await shared_fixtures.seed_shared(db)
    dispatched: list[GatewayMessageRecord] = []

    async def uncertain(
        _agent: AgentRecord, message: GatewayMessageRecord, *, timing: TurnTimeline | None = None
    ) -> UncertainOutcome:
        assert get_connection() is None
        dispatched.append(message)
        return UncertainOutcome(str(message.id), "unknown", None, "connection_lost")

    monkeypatch.setattr(openclaw_gateway_rpc, "execute_gateway_turn", uncertain)
    # When the newest notification wakes the guest but admission is uncertain.
    await openclaw_routing._run_inbox_wake(db.agent, messages[-1].content, messages[-1].id)
    # Then only the oldest input was dispatched, without holding database locks.
    assert [message.id for message in dispatched] == [messages[0].id]
    assert await claim_ready_shared(SharedDispatchRequest(db.agent.id, datetime.now(UTC))) is None
    async with transaction(db.second):
        head = await gateway_message_dao.get_for_agent(messages[0].id, db.agent.id)
    assert head is not None
    assert head.status == "delivered"


async def test_shared_http_report_advances_queue_with_same_session_key(pooled_database: ReceiptDatabase) -> None:
    # Given the real gateway router and a real PostgreSQL shared queue.
    db = pooled_database
    messages = await shared_fixtures.seed_shared(db)
    key = "shared-runtime-qa-only"
    async with transaction(db.first):
        await db.first.execute(
            "UPDATE agents SET api_key_hash = %(key)s, status = 'running' WHERE id = %(id)s",
            {"key": hashlib.sha256(key.encode()).hexdigest(), "id": db.agent.id},
        )
    app = FastAPI()
    app.include_router(gateway.router, prefix="/api")
    first = await claim_ready_shared(SharedDispatchRequest(db.agent.id, datetime.now(UTC)))
    assert first is not None
    assert first.id == messages[0].id
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://qa.local") as client:
        # When an unauthenticated and then the authenticated guest reports the first turn.
        rejected = await client.post(
            "/api/gateway/report",
            headers={"X-Api-Key": "wrong"},
            json={"message_id": str(messages[0].id), "result": "forged"},
        )
        report = await client.post(
            "/api/gateway/report",
            headers={"X-Api-Key": key},
            json={"message_id": str(messages[0].id), "result": "first HTTP answer"},
        )
    second = await claim_ready_shared(SharedDispatchRequest(db.agent.id, datetime.now(UTC)))
    # Then authentication fails closed and the next input uses the same native session.
    assert rejected.status_code == 401
    assert report.status_code == 200
    assert second is not None
    assert second.id == messages[1].id
    assert openclaw_turns.runtime_session_key(first) == openclaw_turns.runtime_session_key(second)
