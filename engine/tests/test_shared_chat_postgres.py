from unittest.mock import AsyncMock
from uuid import uuid4

import anyio
import pytest
from fastapi import HTTPException

from app.core.domain.users import UserRole
from app.dao.chat_dao import chat_message_dao, chat_session_dao
from app.db.pool import close_pool, init_pool
from app.db.session import transaction
from app.records.chat import ChatMessageRecord, ChatSessionRecord
from app.records.user import UserRecord
from app.services import chat_submissions
from app.services.chat_submissions import ChatSubmission
from app.services.shared_chat import ensure_canonical_session, is_canonical, read_only_detail

pytest_plugins = ("test_chat_submission_postgres",)


async def test_concurrent_ensure_for_one_pair_queues_on_the_index_and_returns_one_row(receipt_db):
    db = receipt_db
    inserted, release = anyio.Event(), anyio.Event()
    results: dict[str, ChatSessionRecord] = {}

    async def first():
        async with transaction(db.first):
            results["first"] = await ensure_canonical_session(db.agent.id, db.user.id)
            inserted.set()
            await release.wait()

    async def second():
        async with transaction(db.second):
            results["second"] = await ensure_canonical_session(db.agent.id, db.user.id)

    with anyio.fail_after(10):
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(first)
            await inserted.wait()
            tasks.start_soon(second)
            while True:
                blockers = await db.first.fetchone(
                    "SELECT pg_blocking_pids(%(pid)s) AS blockers", {"pid": db.second.raw.info.backend_pid}
                )
                if blockers and blockers["blockers"]:
                    break
                await anyio.lowlevel.checkpoint()
            release.set()

    assert results["first"].id == results["second"].id
    assert results["first"].user_id == db.user.id
    async with transaction(db.first):
        rows = await db.first.fetchall(
            "SELECT id FROM chat_sessions WHERE agent_id = %(agent)s AND user_id = %(user)s "
            "AND source_channel = 'web' AND is_primary AND NOT is_group",
            {"agent": db.agent.id, "user": db.user.id},
        )
    assert [row["id"] for row in rows] == [results["first"].id]


async def test_each_user_gets_a_private_canonical_row_and_repeat_calls_return_it(receipt_db):
    db = receipt_db
    other = uuid4()
    async with transaction(db.first):
        await db.first.execute(
            "INSERT INTO users (id, display_name, quota_message_limit) VALUES (%(id)s, 'Other human', 1)",
            {"id": other},
        )
        mine = await ensure_canonical_session(db.agent.id, db.user.id)
        theirs = await ensure_canonical_session(db.agent.id, other)
        stored = await db.first.fetchall(
            "SELECT id, user_id FROM chat_sessions WHERE agent_id = %(agent)s AND is_primary", {"agent": db.agent.id}
        )

    assert mine.id != theirs.id
    assert (mine.user_id, theirs.user_id) == (db.user.id, other)
    assert mine.id != db.session_id
    assert all(is_canonical(row) and row.title == "Conversation" and not row.is_group for row in (mine, theirs))
    assert {(row["id"], row["user_id"]) for row in stored} == {(mine.id, db.user.id), (theirs.id, other)}


async def test_new_turn_on_a_read_only_session_is_rejected_after_the_exact_receipt_replays(receipt_db):
    db = receipt_db
    submission = ChatSubmission(client_submission_id=uuid4(), content="old accepted message")
    original = ChatMessageRecord(
        id=uuid4(),
        agent_id=db.agent.id,
        user_id=db.user.id,
        role="user",
        content=submission.content,
        conversation_id=str(db.session_id),
        client_submission_id=submission.client_submission_id,
        submission_fingerprint=submission.fingerprint(),
    )
    async with transaction(db.first):
        await chat_message_dao.reserve_submission(original)
        replay = await chat_submissions.accept_submission(
            submission, user=db.user, agent=db.agent, session_id=db.session_id, history=[]
        )
        with pytest.raises(HTTPException) as rejection:
            await chat_submissions.accept_submission(
                ChatSubmission(client_submission_id=uuid4(), content="new"),
                user=db.user,
                agent=db.agent,
                session_id=db.session_id,
                history=[],
            )
        canonical = await chat_session_dao.get_primary_platform(agent_id=db.agent.id, user_id=db.user.id)

    assert replay.replayed
    assert replay.message.id == original.id
    assert canonical is not None
    assert canonical.id != db.session_id
    assert rejection.value.status_code == 409
    assert rejection.value.detail == read_only_detail(canonical.id)


async def test_read_only_rejection_precedes_preparation_and_quota(receipt_db, monkeypatch):
    prepare = AsyncMock(side_effect=AssertionError("A read-only session must not prepare"))
    charge = AsyncMock(side_effect=AssertionError("A read-only session must not charge"))
    monkeypatch.setattr(chat_submissions, "prepare_openclaw_message", prepare)
    monkeypatch.setattr(chat_submissions, "charge_conversation_quota", charge)

    with pytest.raises(HTTPException) as rejected:
        async with transaction(receipt_db.first):
            await chat_submissions.accept_submission(
                ChatSubmission(client_submission_id=uuid4(), content="new"),
                user=receipt_db.user,
                agent=receipt_db.agent,
                session_id=receipt_db.session_id,
                history=[],
            )

    assert rejected.value.status_code == 409
    prepare.assert_not_awaited()
    charge.assert_not_awaited()


async def test_refusal_names_the_callers_canonical_session_created_on_its_own_connection(receipt_db):
    db = receipt_db
    viewer_id = uuid4()
    async with transaction(db.first):
        await db.first.execute(
            "INSERT INTO users (id, display_name, quota_message_limit) VALUES (%(id)s, 'Company admin', 1)",
            {"id": viewer_id},
        )
        owners = await ensure_canonical_session(db.agent.id, db.user.id)
    viewer = UserRecord(id=viewer_id, role=UserRole.ORG_ADMIN)

    await init_pool(conninfo=db.conninfo, min_size=1, max_size=2)
    try:
        with pytest.raises(HTTPException) as refused:
            await chat_submissions.accept_submission(
                ChatSubmission(client_submission_id=uuid4(), content="read only"),
                user=viewer,
                agent=db.agent,
                session_id=owners.id,
                history=[],
            )
    finally:
        await close_pool()

    async with transaction(db.second):
        canonical = await chat_session_dao.get_primary_platform(agent_id=db.agent.id, user_id=viewer_id)
        written = await db.second.fetchone(
            "SELECT (SELECT count(*) FROM chat_messages) AS messages, (SELECT count(*) FROM gateway_messages) AS enqueued"
        )
    assert canonical is not None
    assert canonical.id != owners.id
    assert refused.value.status_code == 409
    assert refused.value.detail == read_only_detail(canonical.id)
    assert written == {"messages": 0, "enqueued": 0}
