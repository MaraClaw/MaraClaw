from unittest.mock import AsyncMock
from uuid import uuid4

import anyio
import pytest
from fastapi import HTTPException

from app.api import chat_sessions
from app.dao.chat_dao import chat_message_dao, chat_session_dao
from app.db.session import transaction
from app.records.chat import ChatMessageRecord, ChatSessionRecord
from app.records.user import UserRecord
from app.services import chat_submissions, chat_tickets
from app.services.chat_session_service import ensure_primary_platform_session
from app.services.chat_submissions import ChatSubmission
from app.services.llm.types import OpenAIMessage
from app.services.shared_chat import ensure_shared_session, shared_session_id

pytest_plugins = ("test_chat_submission_postgres",)


async def test_concurrent_resolvers_keep_private_history_and_creator_provenance(receipt_db):
    db = receipt_db
    results: list[ChatSessionRecord] = []

    async def resolve(connection):
        async with transaction(connection):
            results.append(await ensure_shared_session(db.agent.id))

    with anyio.fail_after(10):
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(resolve, db.first)
            tasks.start_soon(resolve, db.second)

    assert len(results) == 2
    assert {row.id for row in results} == {shared_session_id(db.agent.id)}
    assert all(row.user_id == db.agent.creator_id and row.is_primary for row in results)
    async with transaction(db.first):
        archived = await chat_session_dao.get(db.session_id)
        rows = await db.first.fetchall("SELECT id FROM chat_sessions WHERE source_channel = 'shared'")
    assert archived is not None
    assert archived.source_channel == "web"
    assert len(rows) == 1


async def test_resume_default_ticket_and_both_lists_resolve_same_empty_session(receipt_db, monkeypatch):
    db = receipt_db
    monkeypatch.setattr(chat_sessions.agent_dao, "get", chat_session_agent_get(db.agent))
    async with transaction(db.first):
        shared = await ensure_primary_platform_session(db.agent.id, uuid4())
        resumed = await chat_sessions.create_session(
            db.agent.id, chat_sessions.CreateSessionIn(title="Ignored"), db.user
        )
        resolved = await chat_tickets.resolve_session(chat_tickets.ChatTicketRequest(agent_id=db.agent.id), db.user.id)
        mine = await chat_sessions.list_sessions(db.agent.id, "mine", db.user)
        all_sessions = await chat_sessions.list_sessions(db.agent.id, "all", db.user)

    assert resumed.id == str(shared.id) == str(resolved)
    assert resumed.source_channel == "shared"
    assert resumed.is_primary
    for rows in (mine, all_sessions):
        canonical = [row for row in rows if row.id == str(shared.id)]
        assert len(canonical) == 1
        assert canonical[0].message_count == canonical[0].unread_count == 0


def chat_session_agent_get(agent):
    async def get(_agent_id):
        return agent

    return get


async def test_shared_history_visible_to_other_human_without_global_read_marker(receipt_db):
    db = receipt_db
    viewer = UserRecord(id=uuid4(), role="member")
    async with transaction(db.first):
        shared = await ensure_shared_session(db.agent.id)
        await chat_message_dao.create(
            obj_in={
                "agent_id": db.agent.id,
                "user_id": db.user.id,
                "role": "user",
                "content": "shared hello",
                "conversation_id": str(shared.id),
            }
        )

        messages = await chat_sessions.get_session_messages(db.agent.id, shared.id, 20, None, viewer)
        stored = await chat_session_dao.get(shared.id)

    assert [message["content"] for message in messages] == ["shared hello"]
    assert stored is not None
    assert stored.last_read_at_by_user is None


async def test_shared_delete_forbidden_but_legacy_delete_preserved(receipt_db):
    db = receipt_db
    async with transaction(db.first):
        shared = await ensure_shared_session(db.agent.id)
    with pytest.raises(HTTPException) as rejection:
        async with transaction(db.first):
            await chat_sessions.delete_session(db.agent.id, shared.id, db.user)
    assert rejection.value.status_code == 409
    async with transaction(db.first):
        await chat_sessions.delete_session(db.agent.id, db.session_id, db.user)
        assert await chat_session_dao.get(shared.id) is not None
        assert await chat_session_dao.get(db.session_id) is None


async def test_archive_exact_receipt_replays_but_new_turn_is_rejected(receipt_db):
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
    assert replay.replayed
    assert replay.message.id == original.id
    with pytest.raises(HTTPException) as rejection:
        async with transaction(db.first):
            await chat_submissions.accept_submission(
                ChatSubmission(client_submission_id=uuid4(), content="new"),
                user=db.user,
                agent=db.agent,
                session_id=db.session_id,
                history=[],
            )
    assert rejection.value.status_code == 409
    assert rejection.value.detail == {"reason": "session_archived", "session_id": str(shared_session_id(db.agent.id))}


async def test_second_human_uses_persisted_shared_context_not_socket_or_archive_history(receipt_db, monkeypatch):
    db = receipt_db
    other = UserRecord(id=uuid4(), quota_message_limit=1)
    prepare = chat_submissions.prepare_openclaw_message
    contexts: list[list[OpenAIMessage]] = []

    async def capture(*, agent, content, history):
        contexts.append(history)
        return await prepare(agent=agent, content=content, history=history)

    monkeypatch.setattr(chat_submissions, "prepare_openclaw_message", capture)
    async with transaction(db.first):
        await db.first.execute(
            "INSERT INTO users (id, display_name, quota_message_limit) VALUES (%(id)s, 'Other human', 1)",
            {"id": other.id},
        )
        await chat_message_dao.create(
            obj_in={
                "agent_id": db.agent.id,
                "user_id": db.user.id,
                "role": "user",
                "content": "private history",
                "conversation_id": str(db.session_id),
            }
        )
        shared = await ensure_shared_session(db.agent.id)
        await chat_submissions.accept_submission(
            ChatSubmission(client_submission_id=uuid4(), content="first human"),
            user=db.user,
            agent=db.agent,
            session_id=shared.id,
            history=[{"role": "assistant", "content": "stale socket"}],
        )
    async with transaction(db.second):
        await chat_tickets.authorized_session(
            chat_tickets.ChatTicketBinding(
                user_id=other.id,
                tenant_id=uuid4(),
                agent_id=db.agent.id,
                session_id=shared.id,
                auth_deadline=2_000_000_000,
                auth_generation=0,
            )
        )
        accepted = await chat_submissions.accept_submission(
            ChatSubmission(client_submission_id=uuid4(), content="second human"),
            user=other,
            agent=db.agent,
            session_id=shared.id,
            history=[],
        )
        messages = await chat_sessions.get_session_messages(db.agent.id, shared.id, 20, None, other)
        mine = await chat_sessions.list_sessions(db.agent.id, "mine", other)

    assert contexts == [[], [{"role": "user", "content": "first human"}]]
    assert accepted.message.user_id == other.id
    assert [message["content"] for message in messages] == ["first human", "second human"]
    assert [(row.message_count, row.unread_count) for row in mine] == [(2, 0)]


async def test_archive_rejection_precedes_preparation_and_quota(receipt_db, monkeypatch):
    prepare = AsyncMock(side_effect=AssertionError("Archive must not prepare"))
    charge = AsyncMock(side_effect=AssertionError("Archive must not charge"))
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
