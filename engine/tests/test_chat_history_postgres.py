from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import anyio
import pytest
from psycopg.errors import QueryCanceled
from tests.test_chat_submission_postgres import ReceiptDatabase
from tests.test_redis_cache import FakeRedis

from app.config import get_settings
from app.core import redis_cache
from app.dao.chat_dao import chat_message_dao, chat_session_dao
from app.db.errors import DbError
from app.db.pool import close_pool, init_pool
from app.db.session import transaction
from app.records.chat import ChatSessionRecord
from app.services import chat_history
from app.services.chat_history import HeldHistory, HistoryCursor, get_history_page, get_history_refresh, parse_cursor

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
    assert (exhausted["items"], exhausted["next_cursor"], exhausted["has_more"]) == ([], None, False)
    assert exhausted["revision"] is not None


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
            assert (fresh["items"], fresh["next_cursor"], fresh["has_more"]) == ([], None, False)
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


async def test_late_commit_with_older_created_at_arrives_in_a_delta_that_a_boundary_keyset_alone_would_miss(
    history_db: HistoryDatabase,
):
    # Given a writer whose transaction starts first, then a later transaction that inserts and commits the boundary row.
    h = history_db
    insert = (
        "INSERT INTO chat_messages (id, agent_id, user_id, role, content, conversation_id, created_at) "
        "VALUES (%(id)s, %(agent)s, %(user)s, 'assistant', %(content)s, %(session)s, %(time)s)"
    )

    def row(index: int, content: str) -> dict[str, object]:
        return {
            "id": UUID(int=index),
            "agent": h.db.agent.id,
            "user": h.db.user.id,
            "content": content,
            "session": str(h.session.id),
            "time": h.timestamp + timedelta(days=1) if index == 100 else h.timestamp - timedelta(days=365),
        }

    async with transaction(h.db.second):
        await h.db.second.execute("SELECT 1")  # Fixes this writer's transaction-start created_at.
        async with transaction(h.db.first):
            await h.db.first.execute(insert, row(100, "boundary"))
        async with transaction(h.db.first):
            seen = await get_history_page(h.session, 50, None, agent_id=h.db.agent.id)
        # When the earlier-starting writer inserts and commits after the client already holds the boundary.
        await h.db.second.execute(insert, row(101, "late"))
    held = HeldHistory(revision=str(seen["revision"]), created_at=h.timestamp, message_id=UUID(int=100))
    async with transaction(h.db.first):
        stamps = {
            row["content"]: row["created_at"]
            for row in await h.db.first.fetchall(
                "SELECT content, created_at FROM chat_messages WHERE content IN ('boundary', 'late')"
            )
        }
        held = held.model_copy(update={"created_at": stamps["boundary"]})
        keyset = await chat_message_dao.list_session_after(
            conversation_id=str(h.session.id), after=(held.created_at, held.message_id), limit=10
        )
        refresh = await get_history_refresh(h.session, 2, held, agent_id=h.db.agent.id)
    # Then the late row sorts before the boundary, a plain after-boundary read cannot see it, and the delta does.
    assert isinstance(stamps["late"], datetime)
    assert isinstance(stamps["boundary"], datetime)
    assert stamps["late"] < stamps["boundary"]
    assert [message.id for message in keyset] == [UUID(int=100)]
    assert refresh["kind"] == "delta"
    assert [item["content"] for item in refresh["items"]] == ["late", "boundary"]
    assert refresh["revision"] != held.revision


async def test_revision_decides_unchanged_and_boundary_state_decides_delta_or_reset(history_db: HistoryDatabase):
    # Given the newest fixture message as the client's boundary at the current revision.
    h = history_db
    async with transaction(h.db.first):
        page = await get_history_page(h.session, 50, None, agent_id=h.db.agent.id)
        held = HeldHistory(revision=str(page["revision"]), created_at=h.timestamp, message_id=UUID(int=5))
        same = await get_history_refresh(h.session, 50, held, agent_id=h.db.agent.id)
    # When a writer commits a new row, then the boundary row is deleted.
    async with transaction(h.db.second):
        await h.db.second.execute(
            "INSERT INTO chat_messages (id, agent_id, user_id, role, content, conversation_id, created_at) "
            "VALUES (%(id)s, %(agent)s, %(user)s, 'assistant', 'six', %(session)s, %(time)s)",
            {
                "id": UUID(int=6),
                "agent": h.db.agent.id,
                "user": h.db.user.id,
                "session": str(h.session.id),
                "time": h.timestamp,
            },
        )
    async with transaction(h.db.first):
        delta = await get_history_refresh(h.session, 50, held, agent_id=h.db.agent.id)
        gap = await get_history_refresh(h.session, 1, held, agent_id=h.db.agent.id)
    async with transaction(h.db.second):
        await h.db.second.execute("DELETE FROM chat_messages WHERE id = %(id)s", {"id": UUID(int=5)})
    async with transaction(h.db.first):
        gone = await get_history_refresh(h.session, 50, held, agent_id=h.db.agent.id)
    # Then each poll returns the response its state calls for, the revision always read before the rows.
    assert same == {"kind": "unchanged", "revision": held.revision}
    assert delta["kind"] == "delta"
    assert [item["message_id"] for item in delta["items"]] == [str(UUID(int=index)) for index in (5, 6)]
    assert gap["kind"] == "reset"
    assert [item["message_id"] for item in gap["page"]["items"]] == [str(UUID(int=6))]
    assert gone["kind"] == "reset"
    assert gone["page"]["revision"] not in (None, held.revision)


async def test_old_message_update_is_delivered_with_its_complete_new_projection(history_db: HistoryDatabase):
    h = history_db
    async with transaction(h.db.first):
        page = await get_history_page(h.session, 2, None, agent_id=h.db.agent.id)
        held = HeldHistory(revision=str(page["revision"]), created_at=h.timestamp, message_id=UUID(int=5))
    async with transaction(h.db.second):
        await h.db.second.execute(
            "UPDATE chat_messages SET content = %(content)s WHERE id = %(id)s",
            {"id": UUID(int=1), "content": 'Before\n```tool_code\nlookup\n```\n```json\n{"q":"x"}\n```\nAfter'},
        )
    async with transaction(h.db.first):
        refresh = await get_history_refresh(h.session, 2, held, agent_id=h.db.agent.id)
    assert refresh["kind"] == "delta"
    assert [entry["entry_id"] for entry in refresh["items"]] == [
        *(f"{UUID(int=1)}:{index}" for index in range(3)),
        str(UUID(int=5)),
    ]


async def test_commit_between_revision_and_rows_is_still_discoverable_next_poll(
    history_db: HistoryDatabase, monkeypatch
):
    h = history_db
    async with transaction(h.db.first):
        page = await get_history_page(h.session, 2, None, agent_id=h.db.agent.id)
    held = HeldHistory(revision=str(page["revision"]), created_at=h.timestamp, message_id=UUID(int=5))
    async with transaction(h.db.second):
        await h.db.second.execute(
            "UPDATE chat_messages SET content = 'first edit' WHERE id = %(id)s", {"id": UUID(int=5)}
        )
        before_rows = await chat_message_dao.history_revision(h.session.id)
    original = chat_message_dao.list_session_changes

    async def racing_changes(**kwargs):
        async with transaction(h.db.second):
            await h.db.second.execute(
                "UPDATE chat_messages SET content = 'late edit' WHERE id = %(id)s", {"id": UUID(int=1)}
            )
        return await original(**kwargs)

    with monkeypatch.context() as scoped:
        scoped.setattr(chat_message_dao, "list_session_changes", racing_changes)
        async with transaction(h.db.first):
            refresh = await get_history_refresh(h.session, 2, held, agent_id=h.db.agent.id)
    assert refresh["kind"] == "delta"
    assert refresh["revision"] == before_rows
    async with transaction(h.db.first):
        next_poll = await get_history_refresh(
            h.session, 2, held.model_copy(update={"revision": refresh["revision"]}), agent_id=h.db.agent.id
        )
    assert next_poll["kind"] == "delta"
    assert [entry["content"] for entry in next_poll["items"]] == ["late edit", "first edit"]


async def test_agent_conversation_index_serves_recent_history_in_order(receipt_db: ReceiptDatabase):
    # Given 20 agents with 10 conversations of 25 messages each, analyzed.
    db = receipt_db
    async with transaction(db.first):
        await db.first.execute(
            "INSERT INTO agents (id, name, creator_id, role_description, max_triggers, min_poll_interval_min, "
            "webhook_rate_limit, access_mode, company_access_level, heartbeat_interval_minutes) "
            "SELECT gen_random_uuid(), 'Peer ' || n, %(user)s, '', 20, 5, 5, 'private', 'use', 240 "
            "FROM generate_series(1, 19) AS n",
            {"user": db.user.id},
        )
        await db.first.execute(
            "INSERT INTO chat_messages (id, agent_id, user_id, role, content, conversation_id) "
            "SELECT gen_random_uuid(), a.id, %(user)s, 'user', 'message', a.id::text || '-' || c "
            "FROM agents AS a, generate_series(1, 10) AS c, generate_series(1, 25) AS m",
            {"user": db.user.id},
        )
        await db.first.execute("ANALYZE chat_messages")
        await db.first.execute("SET LOCAL enable_seqscan = off")
        rows = await db.first.fetchall(
            "EXPLAIN SELECT id FROM chat_messages WHERE agent_id = %(agent)s "
            "AND conversation_id = %(conversation)s ORDER BY created_at DESC LIMIT 50",
            {"agent": db.agent.id, "conversation": f"{db.agent.id}-1"},
        )
    plan = "\n".join(str(next(iter(row.values()))) for row in rows)
    # Then the recent-history read walks one index in order instead of sorting a bitmap of the whole conversation.
    assert "ix_chat_messages_agent_conv_created" in plan, plan
    assert "Sort" not in plan, plan


_JOURNAL_REVISIONS = "SELECT revision FROM chat_history_changes WHERE session_id = %(session)s ORDER BY recorded_at"


async def _backdate_journal(h: HistoryDatabase, recorded_at: datetime, revisions: list[object] | None = None) -> None:
    async with transaction(h.db.first):
        await h.db.first.execute(
            "UPDATE chat_history_changes SET recorded_at = %(at)s "
            "WHERE session_id = %(session)s AND (%(all)s OR revision = ANY(%(revisions)s))",
            {"at": recorded_at, "session": h.session.id, "all": revisions is None, "revisions": revisions or []},
        )


async def test_purge_deletes_at_most_limit_rows_older_than_the_cutoff(history_db: HistoryDatabase):
    # Given five journal rows: three older than the cutoff, one exactly at it and one current.
    h = history_db
    cutoff = datetime.now(UTC) - timedelta(days=30)
    async with transaction(h.db.first):
        revisions = [
            row["revision"] for row in await h.db.first.fetchall(_JOURNAL_REVISIONS, {"session": h.session.id})
        ]
    await _backdate_journal(h, cutoff - timedelta(days=1), revisions[:3])
    await _backdate_journal(h, cutoff, revisions[3:4])
    # When batches of two are purged until one comes back short.
    async with transaction(h.db.first):
        first = await chat_message_dao.purge_history_changes(older_than=cutoff, limit=2)
    async with transaction(h.db.first):
        second = await chat_message_dao.purge_history_changes(older_than=cutoff, limit=2)
        third = await chat_message_dao.purge_history_changes(older_than=cutoff, limit=2)
        kept = await h.db.first.fetchall(_JOURNAL_REVISIONS, {"session": h.session.id})
    # Then each statement deleted at most the limit, and only rows strictly older than the cutoff.
    assert (first, second, third) == (2, 1, 0)
    assert {row["revision"] for row in kept} == set(revisions[3:])


async def test_purge_takes_no_session_lock_and_skips_rows_another_purge_holds(history_db: HistoryDatabase):
    # Given every journal row expired, and a writer holding the session row while its own purge holds one row.
    h = history_db
    cutoff = datetime.now(UTC) - timedelta(days=30)
    await _backdate_journal(h, cutoff - timedelta(days=1))
    lock_session = "SELECT id FROM chat_sessions WHERE id = %(id)s FOR UPDATE"
    purged: list[int] = []
    finished, release = anyio.Event(), anyio.Event()

    async def purge_and_stay_open() -> None:
        async with transaction(h.db.first):
            purged.append(await chat_message_dao.purge_history_changes(older_than=cutoff, limit=10))
            finished.set()
            await release.wait()

    async with anyio.create_task_group() as group:
        async with transaction(h.db.second):
            await h.db.second.execute(lock_session, {"id": h.session.id})
            held = await chat_message_dao.purge_history_changes(older_than=cutoff, limit=1)
            assert held == 1
            # When another purge runs meanwhile.
            group.start_soon(purge_and_stay_open)
            with anyio.fail_after(5):
                await finished.wait()
        # Then it completed without waiting, and while it is still open the session row locks without waiting.
        async with transaction(h.db.second):
            locked = await h.db.second.fetchall(f"{lock_session} NOWAIT", {"id": h.session.id})
        assert locked == [{"id": h.session.id}]
        release.set()
    async with transaction(h.db.first):
        left = await h.db.first.fetchall(_JOURNAL_REVISIONS, {"session": h.session.id})
    assert purged == [4]
    assert left == []


async def test_purge_runs_on_the_pool_under_its_statement_timeout(
    history_db: HistoryDatabase, monkeypatch: pytest.MonkeyPatch
):
    # Given the engine pool on this schema with a 200 ms statement limit and every journal row expired.
    h = history_db
    cutoff = datetime.now(UTC) - timedelta(days=30)
    await _backdate_journal(h, cutoff - timedelta(days=1))
    with monkeypatch.context() as scoped:
        scoped.setenv("DATABASE_STATEMENT_TIMEOUT_MS", "200")
        get_settings.cache_clear()
        _ = await init_pool(conninfo=h.db.conninfo, min_size=1, max_size=1)
    get_settings.cache_clear()
    try:
        # When another session holds the journal exclusively, so the purge can end only by its own limit.
        await h.db.second.execute("LOCK TABLE chat_history_changes IN ACCESS EXCLUSIVE MODE")
        with anyio.fail_after(5), pytest.raises(DbError) as raised:
            await chat_message_dao.purge_history_changes(older_than=cutoff, limit=10)
        await h.db.second.raw.rollback()
        deleted = await chat_message_dao.purge_history_changes(older_than=cutoff, limit=10)
    finally:
        await close_pool()
    # Then the pooled statement was cancelled by the server, and the same pool purges once the lock is gone.
    assert isinstance(raised.value.orig, QueryCanceled)
    assert deleted == 5


async def test_pruned_journal_keeps_unchanged_and_window_deltas_and_resets_pruned_revisions(
    history_db: HistoryDatabase,
):
    # Given a client holding the head after the fixture (window), one holding the revision after message 3
    # (pruned), and the fixture's journal rows older than the window.
    h = history_db
    cutoff = datetime.now(UTC) - timedelta(days=30)
    async with transaction(h.db.first):
        page = await get_history_page(h.session, 50, None, agent_id=h.db.agent.id)
        third = await h.db.first.fetchval(
            "SELECT revision FROM chat_history_changes WHERE session_id = %(session)s AND message_id = %(id)s",
            {"session": h.session.id, "id": UUID(int=3)},
        )
    window = HeldHistory(revision=str(page["revision"]), created_at=h.timestamp, message_id=UUID(int=5))
    pruned = HeldHistory(revision=str(third), created_at=h.timestamp, message_id=UUID(int=3))
    await _backdate_journal(h, cutoff - timedelta(days=10))
    # And inside the window an insert, an edit of the oldest message and another insert are committed.
    insert = (
        "INSERT INTO chat_messages (id, agent_id, user_id, role, content, conversation_id, created_at) "
        "VALUES (%(id)s, %(agent)s, %(user)s, 'assistant', %(content)s, %(session)s, %(time)s)"
    )
    for write, params in (
        (insert, {"id": UUID(int=6), "content": "six"}),
        ("UPDATE chat_messages SET content = %(content)s WHERE id = %(id)s", {"id": UUID(int=1), "content": "edit"}),
        (insert, {"id": UUID(int=7), "content": "seven"}),
    ):
        async with transaction(h.db.second):
            await h.db.second.execute(
                write,
                {
                    **params,
                    "agent": h.db.agent.id,
                    "user": h.db.user.id,
                    "session": str(h.session.id),
                    "time": h.timestamp,
                },
            )
    async with transaction(h.db.first):
        before = await get_history_refresh(h.session, 50, pruned, agent_id=h.db.agent.id)
        head = await chat_message_dao.history_revision(h.session.id)
    assert before["kind"] == "delta"
    # When the journal is pruned below the cutoff.
    async with transaction(h.db.first):
        deleted = await chat_message_dao.purge_history_changes(older_than=cutoff, limit=100)
    async with transaction(h.db.first):
        fresh = await get_history_page(h.session, 50, None, agent_id=h.db.agent.id)
        at_head = await get_history_refresh(
            h.session, 50, window.model_copy(update={"revision": str(head)}), agent_id=h.db.agent.id
        )
        in_window = await get_history_refresh(h.session, 50, window, agent_id=h.db.agent.id)
        reset = await get_history_refresh(h.session, 50, pruned, agent_id=h.db.agent.id)
    # Then the head is unchanged, the window delta equals the fresh read, and the pruned revision resets.
    changed = {str(UUID(int=index)) for index in (1, 5, 6, 7)}
    assert deleted == 5
    assert at_head == {"kind": "unchanged", "revision": head}
    assert in_window["kind"] == "delta"
    assert in_window["revision"] == fresh["revision"] == head
    assert in_window["items"] == [item for item in fresh["items"] if item["message_id"] in changed]
    assert [item["message_id"] for item in in_window["items"]] == [str(UUID(int=index)) for index in (1, 5, 6, 7)]
    assert reset["kind"] == "reset"
    assert reset["page"] == fresh
    assert [item["message_id"] for item in fresh["items"]] == [str(UUID(int=index)) for index in range(1, 8)]
