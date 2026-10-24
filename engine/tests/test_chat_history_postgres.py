from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from tests.test_chat_submission_postgres import ReceiptDatabase
from tests.test_redis_cache import FakeRedis

from app.core import redis_cache
from app.dao.chat_dao import chat_message_dao, chat_session_dao
from app.db.session import transaction
from app.records.chat import ChatSessionRecord
from app.services import chat_history
from app.services.chat_history import HistoryCursor, get_history_page, parse_cursor

pytest_plugins = ["tests.test_chat_submission_postgres", "tests.test_redis_cache"]


@dataclass(frozen=True, slots=True)
class HistoryDatabase:
    db: ReceiptDatabase
    session: ChatSessionRecord
    participant_id: UUID
    timestamp: datetime


@pytest.fixture
async def history_db(receipt_db: ReceiptDatabase, fake_cache: FakeRedis) -> HistoryDatabase:
    db = receipt_db
    participant = uuid4()
    timestamp = datetime(2026, 10, 24, 12, 0, 0, 123456, tzinfo=UTC)
    async with transaction(db.first):
        await db.first.execute(
            "INSERT INTO participants (id, type, ref_id, display_name) VALUES (%(id)s, 'agent', %(ref)s, 'Original')",
            {"id": participant, "ref": db.agent.id},
        )
        await db.first.execute(
            "UPDATE chat_sessions SET source_channel = 'agent' WHERE id = %(id)s", {"id": db.session_id}
        )
        for index in range(1, 6):
            await db.first.execute(
                "INSERT INTO chat_messages (id, agent_id, user_id, role, content, conversation_id, created_at, participant_id) "
                "VALUES (%(id)s, %(agent)s, %(user)s, 'assistant', %(content)s, %(session)s, %(time)s, %(participant)s)",
                {
                    "id": UUID(int=index),
                    "agent": db.agent.id,
                    "user": db.user.id,
                    "content": f"message {index}",
                    "session": str(db.session_id),
                    "time": timestamp,
                    "participant": participant,
                },
            )
        session = await chat_session_dao.get_for_agent_or_peer(db.session_id, db.agent.id)
    assert session is not None
    return HistoryDatabase(db, session, participant, timestamp)


async def test_real_keyset_preserves_ties_precision_and_exhaustion(history_db: HistoryDatabase):
    # Given tied persisted timestamps and UUIDs, with the real DAO and response cache.
    h = history_db
    async with transaction(h.db.first):
        newest = await get_history_page(h.session, 2, None, agent_id=h.db.agent.id)
    boundary = parse_cursor(newest["next_cursor"], h.db.agent.id, h.session.id)
    assert boundary is not None
    assert boundary.created_at == h.timestamp
    # When older pages are fetched through the SQL tuple boundary.
    async with transaction(h.db.first):
        middle = await get_history_page(h.session, 2, boundary, agent_id=h.db.agent.id)
        last = await get_history_page(
            h.session, 2, parse_cursor(middle["next_cursor"], h.db.agent.id, h.session.id), agent_id=h.db.agent.id
        )
        exhausted = await get_history_page(
            h.session,
            2,
            HistoryCursor(
                agent_id=h.db.agent.id, session_id=h.session.id, created_at=h.timestamp, message_id=UUID(int=1)
            ),
            agent_id=h.db.agent.id,
        )
    # Then SQL, not a fake sorter, visits every equal-timestamp row once and terminates.
    assert [[item["message_id"] for item in page["items"]] for page in (last, middle, newest)] == [
        [str(UUID(int=1))],
        [str(UUID(int=2)), str(UUID(int=3))],
        [str(UUID(int=4)), str(UUID(int=5))],
    ]
    assert last["has_more"] is False
    assert last["next_cursor"] is None
    assert exhausted == {"items": [], "next_cursor": None, "has_more": False}


@pytest.mark.parametrize("mutation", ["insert", "update", "delete", "reassign"])
async def test_committed_mutations_invalidate_already_cached_older_pages(history_db: HistoryDatabase, mutation):
    # Given a cached older page and, for reassignment, a cached empty destination session.
    h = history_db
    destination = ChatSessionRecord(id=uuid4(), agent_id=h.db.agent.id, user_id=h.db.user.id, source_channel="agent")
    boundary = HistoryCursor(
        agent_id=h.db.agent.id, session_id=h.session.id, created_at=h.timestamp, message_id=UUID(int=4)
    )
    page_limit = 10 if mutation == "insert" else 2
    async with transaction(h.db.first):
        await h.db.first.execute(
            "INSERT INTO chat_sessions (id, agent_id, user_id, title, source_channel) "
            "VALUES (%(id)s, %(agent)s, %(user)s, 'Destination', 'agent')",
            {"id": destination.id, "agent": h.db.agent.id, "user": h.db.user.id},
        )
        await get_history_page(h.session, page_limit, boundary, agent_id=h.db.agent.id)
        await get_history_page(destination, 2, None, agent_id=h.db.agent.id)
    # When a separate connection commits a raw edit/delete/insert or DAO move.
    async with transaction(h.db.second):
        match mutation:
            case "insert":
                await h.db.second.execute(
                    "INSERT INTO chat_messages (id, agent_id, user_id, role, content, conversation_id, created_at) "
                    "VALUES (%(id)s, %(agent)s, %(user)s, 'assistant', 'inserted', %(session)s, %(time)s)",
                    {
                        "id": UUID(int=0),
                        "agent": h.db.agent.id,
                        "user": h.db.user.id,
                        "session": str(h.session.id),
                        "time": h.timestamp,
                    },
                )
            case "update":
                await h.db.second.execute(
                    "UPDATE chat_messages SET content = 'edited' WHERE id = %(id)s", {"id": UUID(int=3)}
                )
            case "delete":
                await h.db.second.execute("DELETE FROM chat_messages WHERE id = %(id)s", {"id": UUID(int=3)})
            case "reassign":
                await chat_message_dao.reassign_conversation_id(
                    old_conversation_id=str(h.session.id), new_conversation_id=str(destination.id)
                )
    async with transaction(h.db.first):
        fresh = await get_history_page(h.session, page_limit, boundary, agent_id=h.db.agent.id)
        destination_page = await get_history_page(destination, 2, None, agent_id=h.db.agent.id)
    # Then even older pages observe committed mutations; moves refresh both conversations.
    match mutation:
        case "insert":
            assert [item["content"] for item in fresh["items"]] == ["inserted", "message 1", "message 2", "message 3"]
        case "update":
            assert fresh["items"][-1]["content"] == "edited"
        case "delete":
            assert [item["message_id"] for item in fresh["items"]] == [str(UUID(int=1)), str(UUID(int=2))]
        case "reassign":
            assert fresh == {"items": [], "next_cursor": None, "has_more": False}
            assert [item["message_id"] for item in destination_page["items"]] == [str(UUID(int=4)), str(UUID(int=5))]


@pytest.mark.parametrize(("elapsed", "expected_name"), [(29, "Original"), (30, "Renamed")])
async def test_sender_rename_refreshes_after_bounded_cache_expiry(
    history_db: HistoryDatabase,
    fake_cache: FakeRedis,
    monkeypatch: pytest.MonkeyPatch,
    elapsed: int,
    expected_name: str,
) -> None:
    # Given a cached older page followed by a committed sender rename.
    h = history_db
    boundary = HistoryCursor(
        agent_id=h.db.agent.id, session_id=h.session.id, created_at=h.timestamp, message_id=UUID(int=4)
    )
    async with transaction(h.db.first):
        await get_history_page(h.session, 2, boundary, agent_id=h.db.agent.id)
    async with transaction(h.db.second):
        await h.db.second.execute(
            "UPDATE participants SET display_name = 'Renamed' WHERE id = %(id)s", {"id": h.participant_id}
        )
    original_get = fake_cache.get

    async def get_unexpired(key: str) -> str | None:
        if elapsed >= fake_cache.expires.get(key, 0):
            return None
        return await original_get(key)

    monkeypatch.setattr(fake_cache, "get", get_unexpired)
    # When Redis expiration is simulated without sleeping or changing message revisions.
    async with transaction(h.db.first):
        page = await get_history_page(h.session, 2, boundary, agent_id=h.db.agent.id)
    # Then cached names survive before the TTL, but the renamed sender appears at expiry.
    assert {item.get("sender_name") for item in page["items"]} == {expected_name}


async def test_inflight_old_fill_cannot_poison_new_revision(history_db: HistoryDatabase, monkeypatch):
    # Given a cache fill paused after reading old rows, before writing Redis.
    h = history_db
    original_set = redis_cache.cache_set_json
    fills = 0

    async def commit_before_fill(key: str, value, *, ttl: int) -> bool:
        nonlocal fills
        fills += 1
        if fills == 1:
            async with transaction(h.db.second):
                await h.db.second.execute(
                    "UPDATE chat_messages SET content = 'concurrent' WHERE id = %(id)s", {"id": UUID(int=5)}
                )
        return await original_set(key, value, ttl=ttl)

    monkeypatch.setattr(chat_history, "cache_set_json", commit_before_fill)
    # When a writer commits between the old page read and cache publication.
    async with transaction(h.db.first):
        old = await get_history_page(h.session, 2, None, agent_id=h.db.agent.id)
    async with transaction(h.db.first):
        new = await get_history_page(h.session, 2, None, agent_id=h.db.agent.id)
    # Then the old fill is unreachable after commit, without a Redis invalidation dependency.
    assert old["items"][-1]["content"] == "message 5"
    assert new["items"][-1]["content"] == "concurrent"
    assert fills == 2


async def test_rollback_cached_fingerprint_is_never_reused(history_db: HistoryDatabase):
    # Given a cached page produced inside a transaction that will roll back.
    h = history_db

    async def write_then_rollback():
        async with transaction(h.db.first):
            await h.db.first.execute(
                "UPDATE chat_messages SET content = 'uncommitted' WHERE id = %(id)s", {"id": UUID(int=5)}
            )
            await get_history_page(h.session, 2, None, agent_id=h.db.agent.id)
            raise RuntimeError("rollback")

    with pytest.raises(RuntimeError, match="rollback"):
        await write_then_rollback()
    # When another write commits after rollback.
    async with transaction(h.db.second):
        await h.db.second.execute(
            "UPDATE chat_messages SET content = 'committed' WHERE id = %(id)s", {"id": UUID(int=5)}
        )
    async with transaction(h.db.first):
        fresh = await get_history_page(h.session, 2, None, agent_id=h.db.agent.id)
    # Then neither rollback nor revision reuse can expose uncommitted content.
    assert fresh["items"][-1]["content"] == "committed"
