import json
from contextlib import asynccontextmanager
from uuid import uuid4

import httpx
import pytest
from psycopg.pq import TransactionStatus

from app.dao.gateway_report_delivery_dao import gateway_report_delivery_dao as dao
from app.db import session as db_session
from app.db.session import get_connection, transaction
from app.services import gateway_report_relay as relay
from test_channel_delivery_transport import transport
from test_gateway_report_delivery_postgres import delivery_db as delivery_db, receipt_db as receipt_db


@pytest.fixture
async def relay_db(delivery_db, monkeypatch):
    db, row = delivery_db

    class Pool:
        @asynccontextmanager
        async def connection(self):
            yield db.first.raw

    pool = Pool()
    monkeypatch.setattr(db_session, "get_pool", lambda: pool)
    async with transaction(db.first):
        await db.first.execute(
            "UPDATE chat_sessions SET source_channel = 'slack', external_conv_id = 'slack_C1' WHERE id = %(id)s",
            {"id": db.session_id},
        )
        await db.first.execute(
            "INSERT INTO channel_configs (id, agent_id, channel_type, app_id, app_secret, is_configured, is_connected) "
            "VALUES (%(id)s, %(agent)s, 'slack', 'account', 'live-secret', TRUE, TRUE)",
            {"id": uuid4(), "agent": db.agent.id},
        )
    return db, row


async def test_real_relay_resumes_after_later_chunk_failure_without_replanning(relay_db, monkeypatch):
    db, row = relay_db
    original = "a" * 4000 + "second chunk"
    attempts: list[str] = []
    async with transaction(db.first):
        await db.first.execute(
            "UPDATE gateway_messages SET result = %(text)s WHERE id = %(id)s", {"id": row.message_id, "text": original}
        )

    def respond(request: httpx.Request):
        assert get_connection() is None
        assert db.first.raw.info.transaction_status == TransactionStatus.IDLE
        if request.url.path == "/api/auth.test":
            return httpx.Response(200, json={"ok": True, "team_id": "T1", "bot_id": "B1"})
        attempts.append(json.loads(request.content)["text"])
        return httpx.Response(503, json={}) if len(attempts) == 2 else httpx.Response(200, json={"ok": True})

    transport(monkeypatch, respond)

    assert await relay.relay_batch() == 1
    assert await relay.relay_batch() == 1
    async with transaction(db.second):
        partial = await dao.get(row.id)
        await db.second.execute(
            "UPDATE gateway_messages SET result = 'changed after restart' WHERE id = %(id)s", {"id": row.message_id}
        )
        await db.second.execute(
            "UPDATE gateway_report_deliveries SET available_at = clock_timestamp() WHERE id = %(id)s", {"id": row.id}
        )
    assert await relay.relay_batch() == 1

    async with transaction(db.second):
        delivered = await dao.get(row.id)
    assert partial is not None
    assert partial.next_chunk == 1
    assert partial.delivered_at is None
    assert delivered is not None
    assert delivered.next_chunk == 2
    assert delivered.delivered_at is not None
    assert delivered.im_plan == partial.im_plan
    assert attempts == ["a" * 4000, "second chunk", "second chunk"]


async def test_real_positive_ack_survives_identity_change_during_http(relay_db, monkeypatch):
    db, row = relay_db

    async def respond(request: httpx.Request):
        assert get_connection() is None
        assert db.first.raw.info.transaction_status == TransactionStatus.IDLE
        if request.url.path == "/api/auth.test":
            return httpx.Response(200, json={"ok": True, "team_id": "T1", "bot_id": "B1"})
        tenant_id = uuid4()
        async with transaction(db.second):
            await db.second.execute(
                "INSERT INTO tenants (id, name, slug) VALUES (%(id)s, 'Changed', %(slug)s)",
                {"id": tenant_id, "slug": tenant_id.hex},
            )
            await db.second.execute(
                "UPDATE agents SET tenant_id = %(tenant)s WHERE id = %(id)s", {"tenant": tenant_id, "id": db.agent.id}
            )
        return httpx.Response(200, json={"ok": True})

    transport(monkeypatch, respond)

    assert await relay.relay_batch() == 1

    async with transaction(db.second):
        delivered = await dao.get(row.id)
    assert delivered is not None
    assert delivered.next_chunk == 1
    assert delivered.delivered_at is not None
    assert delivered.terminal_at is None
