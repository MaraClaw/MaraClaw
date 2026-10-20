import os
from collections.abc import AsyncIterator
from dataclasses import dataclass
from uuid import UUID, uuid4

import anyio
import pytest
from fastapi import HTTPException
from psycopg import AsyncConnection
from psycopg.conninfo import make_conninfo
from psycopg.rows import DictRow, dict_row

from app.api import chat_sessions
from app.dao.chat_dao import chat_message_dao, chat_session_dao
from app.db.connection import DbConnection
from app.db.errors import CheckViolationError
from app.db.session import get_connection, transaction
from app.records.agent import AgentRecord
from app.records.chat import ChatMessageRecord
from app.records.user import UserRecord
from app.scripts import bootstrap_db
from app.services import chat_submissions, openclaw_routing
from app.services.chat_submissions import AcceptedSubmission, ChatSubmission


@dataclass(frozen=True, slots=True)
class ReceiptDatabase:
    conninfo: str
    first: DbConnection
    second: DbConnection
    user: UserRecord
    agent: AgentRecord
    session_id: UUID


@pytest.fixture
async def receipt_db(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[ReceiptDatabase]:
    conninfo = os.environ.get("CHAT_SUBMISSION_TEST_DATABASE_URL", "")
    if not conninfo:
        pytest.skip("CHAT_SUBMISSION_TEST_DATABASE_URL is required for isolated PostgreSQL receipt QA")
    schema = f"chat_receipts_{uuid4().hex}"
    async with await AsyncConnection[DictRow].connect(conninfo, autocommit=True, row_factory=dict_row) as admin:
        owner = DbConnection(admin)
        await owner.execute(f'CREATE SCHEMA "{schema}"')
        try:
            isolated = make_conninfo(conninfo, options=f"-csearch_path={schema}")
            async with (
                await AsyncConnection[DictRow].connect(isolated, row_factory=dict_row) as first,
                await AsyncConnection[DictRow].connect(isolated, row_factory=dict_row) as second,
            ):
                db = DbConnection(first)
                async with transaction(db):
                    for _ in range(2):
                        for statement in bootstrap_db._statement_bodies(bootstrap_db.SCHEMA_BASELINE.read_text()):
                            await db.execute(statement)
                    user = UserRecord(id=uuid4(), quota_message_limit=1)
                    agent = AgentRecord(id=uuid4(), name="Receipt QA", creator_id=user.id)
                    session_id = uuid4()
                    await db.execute(
                        "INSERT INTO users (id, display_name, quota_message_limit) VALUES (%(id)s, 'Receipt QA', 1)",
                        {"id": user.id},
                    )
                    await db.execute(
                        "INSERT INTO agents (id, name, creator_id, role_description, max_triggers, "
                        "min_poll_interval_min, webhook_rate_limit, access_mode, company_access_level, "
                        "heartbeat_interval_minutes) VALUES (%(id)s, 'Receipt QA', %(user)s, '', 20, 5, 5, "
                        "'private', 'use', 240)",
                        {"id": agent.id, "user": user.id},
                    )
                    await db.execute(
                        "INSERT INTO chat_sessions (id, agent_id, user_id, title) "
                        "VALUES (%(id)s, %(agent)s, %(user)s, 'Session QA')",
                        {"id": session_id, "agent": agent.id, "user": user.id},
                    )

                async def prepare(*, agent: AgentRecord, content: str, history):
                    return openclaw_routing.PreparedOpenClawMessage(agent, content, "primary", None, None, "receipt-qa")

                async def access(_user: UserRecord, _agent: UUID):
                    return agent, "manage"

                monkeypatch.setattr(chat_submissions, "prepare_openclaw_message", prepare)
                monkeypatch.setattr(chat_sessions, "check_agent_access", access)
                monkeypatch.setattr(openclaw_routing, "_schedule_inbox_wake", lambda *_args: None)
                yield ReceiptDatabase(isolated, db, DbConnection(second), user, agent, session_id)
        finally:
            await owner.execute(f'DROP SCHEMA "{schema}" CASCADE')


@pytest.mark.parametrize("ordering", ["replay", "accept_then_delete", "delete_then_accept"])
async def test_parent_lock_serializes_acceptance_replay_and_deletion(receipt_db, monkeypatch, ordering):
    # Given two real connections and a first operation holding the actual parent lock.
    db = receipt_db
    submission = ChatSubmission(client_submission_id=uuid4(), content="hello")
    locked, attempted, release = anyio.Event(), anyio.Event(), anyio.Event()
    original_lock = chat_session_dao.lock_for_agent
    accepted: list[AcceptedSubmission] = []
    failures: list[int] = []

    async def gated_lock(session_id: UUID, agent_id: UUID):
        if get_connection() is db.second:
            attempted.set()
        row = await original_lock(session_id, agent_id)
        if get_connection() is db.first:
            locked.set()
            await release.wait()
        return row

    monkeypatch.setattr(chat_session_dao, "lock_for_agent", gated_lock)

    async def operate(connection: DbConnection, delete: bool):
        result: AcceptedSubmission | None = None
        try:
            async with transaction(connection):
                if delete:
                    await chat_sessions.delete_session(db.agent.id, db.session_id, db.user)
                else:
                    result = await chat_submissions.accept_submission(
                        submission, user=db.user, agent=db.agent, session_id=db.session_id, history=[]
                    )
            if result is not None:
                accepted.append(result)
        except HTTPException as exc:
            failures.append(exc.status_code)

    # When the competing operation demonstrably blocks in PostgreSQL before the first commits.
    with anyio.fail_after(10):
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(operate, db.first, ordering == "delete_then_accept")
            await locked.wait()
            tasks.start_soon(operate, db.second, ordering == "accept_then_delete")
            await attempted.wait()
            while True:
                blockers = await db.first.fetchone(
                    "SELECT pg_blocking_pids(%(pid)s) AS blockers", {"pid": db.second.raw.info.backend_pid}
                )
                if blockers and blockers["blockers"]:
                    break
                await anyio.lowlevel.checkpoint()
            release.set()

    # Then replay has one receipt/charge/enqueue; either delete ordering leaves no resurrected rows.
    async with transaction(db.first):
        counts = await db.first.fetchone(
            "SELECT (SELECT count(*) FROM chat_messages) AS messages, "
            "(SELECT count(*) FROM chat_sessions) AS sessions, "
            "(SELECT count(*) FROM gateway_messages) AS enqueued, "
            "(SELECT quota_messages_used FROM users WHERE id = %(user)s) AS charged",
            {"user": db.user.id},
        )
    if ordering == "replay":
        assert counts == {"messages": 1, "sessions": 1, "enqueued": 1, "charged": 1}
        assert accepted[0].message.id == accepted[1].message.id
        assert [result.replayed for result in accepted] == [False, True]
        assert not failures
    elif ordering == "accept_then_delete":
        assert counts == {"messages": 0, "sessions": 0, "enqueued": 1, "charged": 1}
        assert len(accepted) == 1
        assert not failures
    else:
        assert counts == {"messages": 0, "sessions": 0, "enqueued": 0, "charged": 0}
        assert not accepted
        assert failures == [403]


async def test_reservation_conflict_preserves_usable_transaction(receipt_db):
    # Given an already reserved scoped UUID.
    db = receipt_db
    candidate = ChatMessageRecord(
        id=uuid4(),
        agent_id=db.agent.id,
        user_id=db.user.id,
        role="user",
        content="hello",
        conversation_id=str(db.session_id),
        client_submission_id=uuid4(),
        submission_fingerprint="v1:" + "a" * 64,
    )
    async with transaction(db.first):
        original = await chat_message_dao.reserve_submission(candidate)
    # When another connection attempts the same scoped reservation.
    async with transaction(db.second):
        duplicate = await chat_message_dao.reserve_submission(candidate)
        existing = await chat_message_dao.find_submission(candidate)
    # Then ON CONFLICT did not abort the transaction or replace the original receipt.
    assert duplicate is None
    assert original is not None
    assert existing is not None
    assert existing.id == original.id


async def test_receipt_metadata_is_immutable_but_content_can_be_scrubbed(receipt_db):
    # Given an accepted user receipt.
    db = receipt_db
    async with transaction(db.first):
        accepted = await chat_submissions.accept_submission(
            ChatSubmission(client_submission_id=uuid4(), content="hello"),
            user=db.user,
            agent=db.agent,
            session_id=db.session_id,
            history=[],
        )
        await db.first.execute(
            "UPDATE chat_messages SET content = 'scrubbed' WHERE id = %(id)s", {"id": accepted.message.id}
        )
    # When a writer attempts to erase the accepted fingerprint.
    with pytest.raises(CheckViolationError):
        async with transaction(db.second):
            await db.second.execute(
                "UPDATE chat_messages SET client_submission_id = NULL, submission_fingerprint = NULL WHERE id = %(id)s",
                {"id": accepted.message.id},
            )
    # Then rollback retained the original receipt identity and mutable display content.
    async with transaction(db.first):
        stored = await chat_message_dao.get(accepted.message.id)
    assert stored is not None
    assert stored.client_submission_id == accepted.message.client_submission_id
    assert stored.submission_fingerprint == accepted.message.submission_fingerprint
    assert stored.content == "scrubbed"


async def test_distinct_submission_uuids_enqueue_identical_content_twice(receipt_db):
    db = receipt_db
    async with transaction(db.first):
        await db.first.execute("UPDATE users SET quota_message_limit = 2 WHERE id = %(id)s", {"id": db.user.id})

    for _ in range(2):
        async with transaction(db.first):
            await chat_submissions.accept_submission(
                ChatSubmission(client_submission_id=uuid4(), content="identical"),
                user=db.user,
                agent=db.agent,
                session_id=db.session_id,
                history=[],
            )

    async with transaction(db.first):
        counts = await db.first.fetchone(
            "SELECT (SELECT count(*) FROM chat_messages) AS messages, "
            "(SELECT count(*) FROM gateway_messages) AS enqueued, "
            "(SELECT quota_messages_used FROM users WHERE id = %(user)s) AS charged",
            {"user": db.user.id},
        )
    assert counts == {"messages": 2, "enqueued": 2, "charged": 2}
