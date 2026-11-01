"""Multi-lane PostgreSQL fixtures for the due-burst reconciler (no tests live here)."""

from collections.abc import AsyncIterator
from dataclasses import replace
from uuid import UUID, uuid4

import pytest
from psycopg import AsyncConnection
from psycopg.rows import DictRow, dict_row
from psycopg_pool import AsyncConnectionPool
from tests.test_chat_submission_postgres import receipt_db
from tests.test_whatsapp_personal_burst_store_fixtures import BurstDatabase, admit, age, anchor, burst_db

from app.db import session
from app.db.session import transaction
from app.records.personal_burst import PersonalBurstBatch
from app.services.shared_chat import shared_session_id

__all__ = ["burst_db", "receipt_db"]


async def add_lane(f: BurstDatabase) -> BurstDatabase:
    """A second agent with its own Shared lane and Personal connection, in the same disposable schema."""
    agent, config = uuid4(), uuid4()
    route = f.route.model_copy(update={"config_id": config, "auth_epoch": uuid4(), "generation": uuid4()})
    scope = replace(
        f.scope,
        agent_id=agent,
        shared_session_id=shared_session_id(agent),
        reply_session_id=uuid4(),
        config_id=config,
        auth_epoch=route.auth_epoch,
        generation=route.generation,
    )
    async with transaction(f.db.first):
        await f.db.first.execute(
            "INSERT INTO agents (id, name, creator_id, role_description, max_triggers, min_poll_interval_min, "
            "webhook_rate_limit, access_mode, company_access_level, heartbeat_interval_minutes) "
            "VALUES (%(id)s, 'Lane QA', %(user)s, '', 20, 5, 5, 'private', 'use', 240)",
            {"id": agent, "user": f.scope.sender_user_id},
        )
        await f.db.first.execute(
            "INSERT INTO channel_configs (id, agent_id, channel_type, app_id, app_secret, encrypt_key, "
            "is_configured, is_connected) VALUES (%(id)s, %(agent)s, 'whatsapp', 'acct', 'x', 'y', TRUE, FALSE)",
            {"id": config, "agent": agent},
        )
        await f.db.first.execute(
            "INSERT INTO chat_sessions (id, agent_id, user_id, title, source_channel, is_primary) "
            "VALUES (%(id)s, %(agent)s, %(user)s, 'Shared', 'shared', TRUE)",
            {"id": scope.shared_session_id, "agent": agent, "user": f.scope.sender_user_id},
        )
        await f.db.first.execute(
            "INSERT INTO chat_sessions (id, agent_id, user_id, title, source_channel, external_conv_id) "
            "VALUES (%(id)s, %(agent)s, %(user)s, 'Reply', 'whatsapp', %(ext)s)",
            {"id": scope.reply_session_id, "agent": agent, "user": f.scope.sender_user_id, "ext": route.external_id()},
        )
    return replace(f, scope=scope, route=route)


async def due_batch(f: BurstDatabase, key: str = "n1") -> PersonalBurstBatch:
    """One collecting batch whose DB-time window has already closed."""
    batch = await admit(f, key, "due")
    await age(f, batch.gateway_id, 10)
    return batch


async def due_lanes(f: BurstDatabase, count: int) -> list[tuple[BurstDatabase, PersonalBurstBatch]]:
    """``count`` lanes (the fixture's own first), each with one due head, oldest ``ready_at`` first."""
    lanes = [f, *[await add_lane(f) for _ in range(count - 1)]]
    return [(lane, await due_batch(lane)) for lane in lanes]


async def occupy(f: BurstDatabase) -> UUID:
    """An unreported delivered shared turn: the lane is single-flight, so nothing behind it can run."""
    async with transaction(f.db.first):
        active = await anchor(f, content="active")
        await f.db.first.execute("UPDATE gateway_messages SET status = 'delivered' WHERE id = %(id)s", {"id": active})
    return active


async def deliver(f: BurstDatabase, gateway_id: UUID) -> None:
    """Dispatch outcome: the head leaves the pending FIFO (claiming marks it delivered)."""
    async with transaction(f.db.first):
        await f.db.first.execute(
            "UPDATE gateway_messages SET status = 'delivered' WHERE id = %(id)s", {"id": gateway_id}
        )


@pytest.fixture
async def real_pool(burst_db: BurstDatabase, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[None]:
    """A real pool on the disposable schema, so scans run with NO ambient connection like the worker's."""
    async with AsyncConnectionPool[AsyncConnection[DictRow]](
        burst_db.db.conninfo, min_size=1, max_size=4, open=False, kwargs={"row_factory": dict_row}
    ) as pool:
        await pool.wait()
        monkeypatch.setattr(session, "get_pool", lambda: pool)
        yield
