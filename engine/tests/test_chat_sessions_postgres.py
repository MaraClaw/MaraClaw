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

BASE = datetime(2030, 1, 1, tzinfo=UTC)


@dataclass(frozen=True)
class Seeded:
    agent_id: UUID
    creator_id: UUID
    other_id: UUID
    admin_id: UUID
    peer_id: UUID
    client: httpx.AsyncClient
    admin: httpx.AsyncClient
    other: httpx.AsyncClient


def _client(viewer_id: UUID) -> httpx.AsyncClient:
    app = FastAPI()
    app.include_router(chat_sessions.router, dependencies=[Depends(bind_crud_connection)])

    async def viewer() -> UserRecord:
        return await load_user_principal(viewer_id, fresh=True)

    app.dependency_overrides[get_current_user] = viewer
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://sessions-qa")


def _viewer(seeded: Seeded, scope: str) -> httpx.AsyncClient:
    """Only an org admin may list every user's sessions; scope=mine is the creator's own view."""
    return seeded.admin if scope == "all" else seeded.client


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
            creator_id, other_id, admin_id, agent_id, peer_id = uuid4(), uuid4(), uuid4(), uuid4(), uuid4()
            for user_id, role in ((creator_id, "member"), (other_id, "member"), (admin_id, "org_admin")):
                await db.execute(
                    "INSERT INTO users (id, display_name, role) VALUES (%(id)s, %(name)s, %(role)s)",
                    {"id": user_id, "name": f"User {str(user_id)[:4]}", "role": role},
                )
            for key, name in ((agent_id, "Paged agent"), (peer_id, "Peer agent")):
                await db.execute(
                    "INSERT INTO agents (id, name, role_description, creator_id, max_triggers, min_poll_interval_min, "
                    "webhook_rate_limit, access_mode, company_access_level, heartbeat_interval_minutes) "
                    "VALUES (%(id)s, %(name)s, 'QA', %(creator)s, 10, 5, 10, 'company', 'use', 30)",
                    {"id": key, "name": name, "creator": creator_id},
                )
            await init_pool(conninfo=make_conninfo(conninfo, options=f"-csearch_path={schema}"), min_size=1, max_size=4)

            async with _client(creator_id) as client, _client(admin_id) as admin, _client(other_id) as other:
                yield Seeded(agent_id, creator_id, other_id, admin_id, peer_id, client, admin, other)
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
        await _say(seeded, sid, owner or seeded.creator_id)
    return sid


async def _say(seeded: Seeded, session_id: UUID, owner_id: UUID) -> None:
    async with connection_ctx() as db:
        await db.execute(
            "INSERT INTO chat_messages (id, agent_id, user_id, role, content, conversation_id) "
            "VALUES (%(id)s, %(agent)s, %(user)s, 'user', 'hello', %(conversation)s)",
            {"id": uuid4(), "agent": seeded.agent_id, "user": owner_id, "conversation": str(session_id)},
        )


async def _resume(seeded: Seeded, client: httpx.AsyncClient) -> httpx.Response:
    return await client.post(f"/api/agents/{seeded.agent_id}/sessions")


async def _user(role: str) -> UUID:
    user_id = uuid4()
    async with connection_ctx() as db:
        await db.execute(
            "INSERT INTO users (id, display_name, role) VALUES (%(id)s, %(name)s, %(role)s)",
            {"id": user_id, "name": f"{role} {str(user_id)[:4]}", "role": role},
        )
    return user_id


async def _traverse(seeded: Seeded, scope: str, limit: int) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    cursor: str | None = None
    while True:
        params = {"scope": scope, "limit": str(limit), **({"cursor": cursor} if cursor is not None else {})}
        response = await _viewer(seeded, scope).get(f"/api/agents/{seeded.agent_id}/sessions/page", params=params)
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
    legacy = await _viewer(seeded, scope).get(f"/api/agents/{seeded.agent_id}/sessions", params={"scope": scope})
    paged = await _traverse(seeded, scope, limit)
    # Then the same rows with the same fields appear once, the caller's own conversation first, in the stated order.
    viewer_id = seeded.admin_id if scope == "all" else seeded.creator_id
    assert legacy.status_code == 200
    assert (paged[0]["user_id"], paged[0]["source_channel"], paged[0]["is_primary"], paged[0]["writable"]) == (
        str(viewer_id),
        "web",
        True,
        True,
    )
    assert {row["id"] for row in paged} == {row["id"] for row in legacy.json()}
    assert len({row["id"] for row in paged}) == len(paged)
    by_id = {row["id"]: row for row in legacy.json()}
    assert all(row == by_id[str(row["id"])] for row in paged)
    keys = [
        (row["last_message_at"] is not None, row["last_message_at"] or "", row["created_at"], row["id"])
        for row in paged[1:]
    ]
    assert keys == sorted(keys, reverse=True)
    assert not [row for row in paged[1:] if row["message_count"] == 0]
    assert all(row["writable"] is False for row in paged[1:] if row["source_channel"] == "web")


async def test_live_cursor_survives_a_row_moving_up_mid_scroll(seeded: Seeded) -> None:
    await _dataset(seeded)
    first = await seeded.admin.get(f"/api/agents/{seeded.agent_id}/sessions/page", params={"scope": "all", "limit": 4})
    shown = [row["id"] for row in first.json()["items"]]
    async with connection_ctx() as db:
        await db.execute(
            "UPDATE chat_sessions SET last_message_at = %(at)s WHERE id = %(id)s",
            {"at": BASE + timedelta(days=3), "id": UUID(shown[1])},
        )
    rest = await seeded.admin.get(
        f"/api/agents/{seeded.agent_id}/sessions/page",
        params={"scope": "all", "limit": 100, "cursor": first.json()["next_cursor"]},
    )
    assert rest.status_code == 200
    assert not set(shown) & {row["id"] for row in rest.json()["items"]}


async def test_live_lookup_returns_sessions_far_beyond_the_first_page(seeded: Seeded) -> None:
    await _dataset(seeded)
    oldest = (await _traverse(seeded, "all", 100))[-1]
    first = await seeded.admin.get(f"/api/agents/{seeded.agent_id}/sessions/page", params={"scope": "all", "limit": 2})
    assert oldest["id"] not in {row["id"] for row in first.json()["items"]}
    found = await seeded.admin.get(f"/api/agents/{seeded.agent_id}/sessions/{oldest['id']}")
    assert found.status_code == 200
    assert found.json() == oldest
    missing = await seeded.admin.get(f"/api/agents/{seeded.agent_id}/sessions/{uuid4()}")
    assert missing.status_code == 404


async def test_each_member_resumes_their_own_conversation_and_cannot_read_the_others(seeded: Seeded) -> None:
    # Given two members of a company agent.
    # When each resumes a conversation, one of them twice.
    mine = await _resume(seeded, seeded.client)
    theirs = await _resume(seeded, seeded.other)
    again = await _resume(seeded, seeded.client)
    # Then the conversations are distinct and private, and the repeat resumes the same one.
    assert (mine.status_code, theirs.status_code, again.status_code) == (201, 201, 201)
    assert mine.json()["id"] != theirs.json()["id"]
    assert again.json()["id"] == mine.json()["id"]
    for body, owner_id in ((mine.json(), seeded.creator_id), (theirs.json(), seeded.other_id)):
        assert (body["user_id"], body["source_channel"], body["is_primary"], body["writable"]) == (
            str(owner_id),
            "web",
            True,
            True,
        )
    foreign = await seeded.other.get(f"/api/agents/{seeded.agent_id}/sessions/{mine.json()['id']}/messages")
    assert foreign.status_code == 403
    own = await seeded.other.get(f"/api/agents/{seeded.agent_id}/sessions/{theirs.json()['id']}/messages")
    assert own.status_code == 200


@pytest.mark.parametrize("role", ["creator", "agent_admin", "platform_admin"])
async def test_only_org_admins_reach_other_users_conversations(seeded: Seeded, role: str) -> None:
    # Given a member's conversation with a message in it.
    theirs = (await _resume(seeded, seeded.other)).json()
    await _say(seeded, UUID(theirs["id"]), seeded.other_id)
    viewer_id = seeded.creator_id if role == "creator" else await _user(role)
    # When the agent creator, an agent admin or a platform admin tries to reach it.
    async with _client(viewer_id) as viewer:
        base = f"/api/agents/{seeded.agent_id}/sessions"
        responses = [
            await viewer.get(f"{base}/{theirs['id']}/messages"),
            await viewer.get(f"{base}/{theirs['id']}/messages/page"),
            await viewer.get(f"{base}/{theirs['id']}"),
            await viewer.get(base, params={"scope": "all"}),
            await viewer.get(f"{base}/page", params={"scope": "all"}),
        ]
        mine = await viewer.get(base)
    # Then every attempt is refused and their own list never shows it.
    assert [response.status_code for response in responses] == [403] * 5
    assert theirs["id"] not in {row["id"] for row in mine.json()}


async def test_org_admin_reads_every_conversation_read_only_and_cannot_delete_them(seeded: Seeded) -> None:
    # Given two members' conversations with messages in them.
    first = (await _resume(seeded, seeded.client)).json()
    second = (await _resume(seeded, seeded.other)).json()
    for body, owner_id in ((first, seeded.creator_id), (second, seeded.other_id)):
        await _say(seeded, UUID(body["id"]), owner_id)
    base = f"/api/agents/{seeded.agent_id}/sessions"
    # When the org admin lists everything, by list and by page, reads one and deletes it.
    listed = await seeded.admin.get(base, params={"scope": "all"})
    paged = await _traverse(seeded, "all", 50)
    history = await seeded.admin.get(f"{base}/{first['id']}/messages")
    deleted = await seeded.admin.delete(f"{base}/{first['id']}")
    # Then both are listed read-only behind the admin's own writable conversation, and neither is deletable.
    for rows in (listed.json(), paged):
        assert rows[0]["user_id"] == str(seeded.admin_id)
        assert rows[0]["writable"] is True
        by_id = {row["id"]: row for row in rows}
        assert (by_id[first["id"]]["writable"], by_id[second["id"]]["writable"]) == (False, False)
    assert history.status_code == 200
    assert [row["content"] for row in history.json()] == ["hello"]
    assert deleted.status_code == 409
    assert deleted.json()["detail"] == {"reason": "canonical_session_required", "session_id": first["id"]}
    assert (await seeded.client.get(f"{base}/{first['id']}/messages")).status_code == 200
