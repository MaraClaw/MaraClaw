import os
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import Depends, FastAPI
from psycopg import AsyncConnection
from psycopg.conninfo import make_conninfo
from psycopg.rows import DictRow, dict_row

from app.api import chat_sessions
from app.core.security import get_current_user, load_user_principal
from app.db import session as session_module
from app.db.connection import DbConnection
from app.db.pool import close_pool, init_pool
from app.db.session import bind_crud_connection, connection_ctx
from app.records.user import UserRecord
from app.scripts.bootstrap_db import SCHEMA_BASELINE, _statement_bodies
from app.services.shared_chat import shared_session_id

BASE = datetime(2030, 1, 1, tzinfo=UTC)


@dataclass(frozen=True)
class Seeded:
    agent_id: UUID
    creator_id: UUID
    other_id: UUID
    peer_id: UUID
    client: httpx.AsyncClient


@pytest.fixture
async def seeded() -> AsyncIterator[Seeded]:
    conninfo = os.environ.get("BOOTSTRAP_TEST_DATABASE_URL", "")
    if not conninfo:
        pytest.skip("BOOTSTRAP_TEST_DATABASE_URL is required for isolated PostgreSQL QA")
    schema = f"chat_sessions_qa_{uuid4().hex}"
    context_token = session_module._conn_ctx.set(None)
    async with await AsyncConnection[DictRow].connect(conninfo, autocommit=True, row_factory=dict_row) as raw:
        db = DbConnection(raw)
        await db.execute(f'CREATE SCHEMA "{schema}"')
        try:
            await db.execute(f'SET search_path TO "{schema}"')
            for _ in range(2):
                for statement in _statement_bodies(SCHEMA_BASELINE.read_text()):
                    await db.execute(statement)
            creator_id, other_id, agent_id, peer_id = uuid4(), uuid4(), uuid4(), uuid4()
            for user_id in (creator_id, other_id):
                await db.execute(
                    "INSERT INTO users (id, display_name) VALUES (%(id)s, %(name)s)",
                    {"id": user_id, "name": f"User {str(user_id)[:4]}"},
                )
            for key, name in ((agent_id, "Paged agent"), (peer_id, "Peer agent")):
                await db.execute(
                    "INSERT INTO agents (id, name, role_description, creator_id, max_triggers, min_poll_interval_min, "
                    "webhook_rate_limit, access_mode, company_access_level, heartbeat_interval_minutes) "
                    "VALUES (%(id)s, %(name)s, 'QA', %(creator)s, 10, 5, 10, 'private', 'use', 30)",
                    {"id": key, "name": name, "creator": creator_id},
                )
            await init_pool(conninfo=make_conninfo(conninfo, options=f"-csearch_path={schema}"), min_size=1, max_size=4)

            app = FastAPI()
            app.include_router(chat_sessions.router, dependencies=[Depends(bind_crud_connection)])

            async def creator() -> UserRecord:
                return await load_user_principal(creator_id, fresh=True)

            app.dependency_overrides[get_current_user] = creator
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://sessions-qa"
            ) as client:
                yield Seeded(agent_id, creator_id, other_id, peer_id, client)
        finally:
            await close_pool()
            await db.execute(f'DROP SCHEMA "{schema}" CASCADE')
            session_module._conn_ctx.reset(context_token)


async def _session(
    seeded: Seeded,
    *,
    minutes: int | None,
    created: int,
    messages: int = 1,
    owner: UUID | None = None,
    channel: str = "web",
    agent_id: UUID | None = None,
    peer_agent_id: UUID | None = None,
    session_id: UUID | None = None,
) -> UUID:
    sid = session_id or uuid4()
    async with connection_ctx() as db:
        await db.execute(
            "INSERT INTO chat_sessions (id, agent_id, user_id, title, source_channel, peer_agent_id, created_at, "
            "last_message_at) VALUES (%(id)s, %(agent)s, %(user)s, %(title)s, %(channel)s, %(peer)s, %(created)s, "
            "%(last)s)",
            {
                "id": sid,
                "agent": agent_id or seeded.agent_id,
                "user": owner or seeded.creator_id,
                "title": f"Session {created}",
                "channel": channel,
                "peer": peer_agent_id,
                "created": BASE + timedelta(seconds=created),
                "last": None if minutes is None else BASE + timedelta(minutes=minutes),
            },
        )
        for _ in range(messages):
            await db.execute(
                "INSERT INTO chat_messages (id, agent_id, user_id, role, content, conversation_id) "
                "VALUES (%(id)s, %(agent)s, %(user)s, 'user', 'hello', %(conversation)s)",
                {"id": uuid4(), "agent": seeded.agent_id, "user": owner or seeded.creator_id, "conversation": str(sid)},
            )
    return sid


async def _traverse(seeded: Seeded, scope: str, limit: int) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    cursor: str | None = None
    while True:
        params = {"scope": scope, "limit": str(limit), **({"cursor": cursor} if cursor is not None else {})}
        response = await seeded.client.get(f"/api/agents/{seeded.agent_id}/sessions/page", params=params)
        assert response.status_code == 200
        body = response.json()
        rows.extend(body["items"])
        next_cursor = body["next_cursor"]
        assert next_cursor is None or isinstance(next_cursor, str)
        cursor = next_cursor
        if cursor is None:
            return rows


async def _dataset(seeded: Seeded) -> None:
    # Ties on last_message_at and created_at, NULL last_message_at, empty sessions, peer-agent and foreign rows.
    for index in range(14):
        owner = seeded.creator_id if index % 2 else seeded.other_id
        await _session(
            seeded,
            minutes=None if index % 4 == 0 else index // 3,
            created=index // 2,
            owner=owner,
            messages=0 if index in (3, 9) else 1,
        )
    await _session(
        seeded,
        minutes=2,
        created=1,
        owner=seeded.other_id,
        channel="agent",
        agent_id=seeded.peer_id,
        peer_agent_id=seeded.agent_id,
    )
    await _session(seeded, minutes=2, created=1, channel="trigger")


@pytest.mark.parametrize("scope", ["mine", "all"])
@pytest.mark.parametrize("limit", [1, 3, 50])
async def test_live_pages_concatenate_to_todays_list_rows(seeded: Seeded, scope: str, limit: int) -> None:
    # Given a quiet dataset with ties, NULL last_message_at, empty sessions and peer-agent rows.
    await _dataset(seeded)
    # When paging through the real route and fetching today's list.
    legacy = await seeded.client.get(f"/api/agents/{seeded.agent_id}/sessions", params={"scope": scope})
    paged = await _traverse(seeded, scope, limit)
    # Then the same rows with the same fields appear once, shared first, in the stated order.
    assert legacy.status_code == 200
    assert paged[0]["id"] == str(shared_session_id(seeded.agent_id))
    assert {row["id"] for row in paged} == {row["id"] for row in legacy.json()}
    assert len({row["id"] for row in paged}) == len(paged)
    by_id = {row["id"]: row for row in legacy.json()}
    assert all(row == by_id[str(row["id"])] for row in paged)
    keys = [
        (row["last_message_at"] is not None, row["last_message_at"] or "", row["created_at"], row["id"])
        for row in paged[1:]
    ]
    assert keys == sorted(keys, reverse=True)
    assert not [row for row in paged if row["message_count"] == 0 and row["source_channel"] != "shared"]


async def test_live_cursor_survives_a_row_moving_up_mid_scroll(seeded: Seeded) -> None:
    await _dataset(seeded)
    first = await seeded.client.get(f"/api/agents/{seeded.agent_id}/sessions/page", params={"scope": "all", "limit": 4})
    shown = [row["id"] for row in first.json()["items"]]
    async with connection_ctx() as db:
        await db.execute(
            "UPDATE chat_sessions SET last_message_at = %(at)s WHERE id = %(id)s",
            {"at": BASE + timedelta(days=3), "id": UUID(shown[1])},
        )
    rest = await seeded.client.get(
        f"/api/agents/{seeded.agent_id}/sessions/page",
        params={"scope": "all", "limit": 100, "cursor": first.json()["next_cursor"]},
    )
    assert rest.status_code == 200
    assert not set(shown) & {row["id"] for row in rest.json()["items"]}


async def test_live_lookup_returns_sessions_far_beyond_the_first_page(seeded: Seeded) -> None:
    await _dataset(seeded)
    oldest = (await _traverse(seeded, "all", 100))[-1]
    first = await seeded.client.get(f"/api/agents/{seeded.agent_id}/sessions/page", params={"scope": "all", "limit": 2})
    assert oldest["id"] not in {row["id"] for row in first.json()["items"]}
    found = await seeded.client.get(f"/api/agents/{seeded.agent_id}/sessions/{oldest['id']}")
    assert found.status_code == 200
    assert found.json() == oldest
    missing = await seeded.client.get(f"/api/agents/{seeded.agent_id}/sessions/{uuid4()}")
    assert missing.status_code == 404
