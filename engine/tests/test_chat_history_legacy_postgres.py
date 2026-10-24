from collections.abc import AsyncIterator
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from psycopg.errors import UndefinedColumn
from tests.test_chat_submission_postgres import ReceiptDatabase

from app.api import chat_sessions
from app.core.security import get_current_user
from app.dao.chat_dao import chat_message_dao
from app.db.errors import DbError
from app.db.session import transaction
from app.scripts import bootstrap_db
from app.services import chat_history

pytest_plugins = ["tests.test_chat_submission_postgres"]
pytestmark = pytest.mark.parametrize("receipt_db", ["legacy"], indirect=True)


@pytest.fixture
async def legacy_http(receipt_db: ReceiptDatabase, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[AsyncClient]:
    db = receipt_db
    async with transaction(db.first):
        for index in (1, 2):
            await db.first.execute(
                "INSERT INTO chat_messages (id, agent_id, user_id, role, content, conversation_id, created_at) "
                "VALUES (%(id)s, %(agent)s, %(user)s, 'assistant', %(content)s, %(session)s, '2026-10-24T12:00:00Z')",
                {
                    "id": UUID(int=index),
                    "agent": db.agent.id,
                    "user": db.user.id,
                    "content": f"retained {index}",
                    "session": str(db.session_id),
                },
            )

    async def forbidden_cache(*_args: object, **_kwargs: object) -> None:
        pytest.fail("Legacy history must bypass both cache reads and cache writes")

    monkeypatch.setattr(chat_history, "cache_get_json", forbidden_cache)
    monkeypatch.setattr(chat_history, "cache_set_json", forbidden_cache)
    app = FastAPI()
    app.include_router(chat_sessions.router)
    app.dependency_overrides[get_current_user] = lambda: db.user
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://history.test") as client:
        yield client


async def test_pre_column_history_reads_uncached_over_http(receipt_db: ReceiptDatabase, legacy_http: AsyncClient):
    # Given retained messages in an actual pre-column schema and forbidden cache access.
    db = receipt_db
    url = f"/api/agents/{db.agent.id}/sessions/{db.session_id}/messages/page"
    async with transaction(db.first):
        first = await legacy_http.get(url, params={"limit": 1})
    assert first.status_code == 200
    assert first.json()["items"][0]["content"] == "retained 2"
    async with transaction(db.second):
        await db.second.execute("UPDATE chat_messages SET content = 'edited' WHERE id = %(id)s", {"id": UUID(int=2)})
    # When the same page and an older cursor are requested after a committed edit.
    async with transaction(db.first):
        repeated = await legacy_http.get(url, params={"limit": 1})
        older = await legacy_http.get(url, params={"limit": 1, "cursor": first.json()["next_cursor"]})
    # Then both HTTP pages succeed without Redis and the latest reflects the edit immediately.
    assert repeated.status_code == older.status_code == 200
    assert repeated.json()["items"][0]["content"] == "edited"
    assert [item["content"] for item in older.json()["items"]] == ["retained 1"]
    assert older.json()["next_cursor"] is None


@pytest.mark.parametrize("existing_session", [True, False])
async def test_legacy_revision_is_none_not_a_string(receipt_db: ReceiptDatabase, existing_session: bool):
    # Given a pre-column table, with or without the requested session.
    db = receipt_db
    session_id = db.session_id if existing_session else uuid4()
    # When the real DAO reads the optional revision.
    async with transaction(db.first):
        revision = await chat_message_dao.history_revision(session_id)
    # Then absence is the cache-bypass sentinel, never the string "None".
    assert revision is None


@pytest.mark.parametrize("previous_broken_trigger", [False, True])
async def test_baseline_rerun_preserves_legacy_data_and_message_writes(
    receipt_db: ReceiptDatabase, legacy_http: AsyncClient, previous_broken_trigger: bool
):
    # Given retained pre-column data, optionally with the formerly unconditional trigger.
    db = receipt_db
    baseline = bootstrap_db._statement_bodies(bootstrap_db.SCHEMA_BASELINE.read_text())
    if previous_broken_trigger:
        async with transaction(db.first):
            for statement in baseline:
                if statement.startswith("CREATE OR REPLACE FUNCTION refresh_chat_history_revision()"):
                    await db.first.execute(statement)
            await db.first.execute(
                "CREATE TRIGGER chat_history_revision_changed AFTER INSERT OR UPDATE OR DELETE ON chat_messages "
                "FOR EACH ROW EXECUTE FUNCTION refresh_chat_history_revision()"
            )
        with pytest.raises(DbError) as failure:
            async with transaction(db.first):
                await db.first.execute(
                    "UPDATE chat_messages SET content = 'blocked' WHERE id = %(id)s", {"id": UUID(int=1)}
                )
        assert isinstance(failure.value.orig, UndefinedColumn)
    # When the complete current baseline is rerun twice on the existing schema.
    async with transaction(db.first):
        for _ in range(2):
            for statement in baseline:
                await db.first.execute(statement)
    async with transaction(db.second):
        await db.second.execute(
            "INSERT INTO chat_messages (id, agent_id, user_id, role, content, conversation_id) "
            "VALUES (%(id)s, %(agent)s, %(user)s, 'assistant', 'new', %(session)s)",
            {"id": UUID(int=3), "agent": db.agent.id, "user": db.user.id, "session": str(db.session_id)},
        )
        await db.second.execute("UPDATE chat_messages SET content = 'edited' WHERE id = %(id)s", {"id": UUID(int=3)})
        await db.second.execute("DELETE FROM chat_messages WHERE id = %(id)s", {"id": UUID(int=3)})
    url = f"/api/agents/{db.agent.id}/sessions/{db.session_id}/messages/page"
    async with transaction(db.first):
        response = await legacy_http.get(url)
        revision = await chat_message_dao.history_revision(db.session_id)
    # Then old data survives, all three writes work, and history remains available uncached.
    assert response.status_code == 200
    assert [item["content"] for item in response.json()["items"]] == ["retained 1", "retained 2"]
    assert revision is None
