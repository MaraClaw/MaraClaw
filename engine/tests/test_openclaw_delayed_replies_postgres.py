from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from sqlite3 import Connection
from uuid import uuid4

import anyio
import pytest

import test_shared_gateway_postgres as shared_fixtures
from app.dao.chat_dao import chat_message_dao
from app.dao.gateway_message_dao import gateway_message_dao
from app.dao.openclaw_reply_dao import openclaw_reply_dao
from app.db.session import transaction
from app.records.gateway_message import GatewayMessageRecord
from app.services import openclaw_routing
from app.services.openclaw_delayed_replies import DelayedReplyRecovery
from app.services.openclaw_turns import runtime_session_key
from test_chat_submission_postgres import ReceiptDatabase
from test_openclaw_delayed_replies import append, transcript

receipt_database = shared_fixtures.receipt_database
pooled_database = shared_fixtures.pooled_database


@dataclass(frozen=True, slots=True)
class RecoveryCase:
    db: ReceiptDatabase
    native: GatewayMessageRecord
    root: Path
    sqlite: Connection
    events: list[dict[str, object]]


@pytest.fixture
async def recovery_case(
    pooled_database: ReceiptDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[RecoveryCase]:
    db = pooled_database
    prepared = openclaw_routing.PreparedOpenClawMessage(db.agent, "hello", "primary", "provider/secret-model-v3", "qa")
    async with transaction(db.first):
        native = await openclaw_routing.enqueue_prepared_openclaw_message(
            prepared,
            sender_user_id=db.user.id,
            conversation_id=str(db.session_id),
        )
        claimed = await gateway_message_dao.claim_ordinary_head(native.id, db.agent.id)
        assert claimed is not None
        native = claimed
        await db.first.execute(
            "UPDATE gateway_messages SET status = 'completed', result = 'Original answer' WHERE id = %(id)s",
            {"id": native.id},
        )
    assert native.reply_binding_id is not None
    root = tmp_path / str(db.agent.id)
    media = root / "media/result.jpg"
    media.parent.mkdir(parents=True)
    media.write_bytes(b"jpeg fixture")
    sqlite = transcript(root / "agents/main/agent/openclaw-agent.sqlite")
    sqlite.execute("INSERT INTO session_windows VALUES ('physical', ?)", (runtime_session_key(native),))
    append(sqlite, "physical", 1, run=str(native.id))
    append(sqlite, "physical", 2)
    events: list[dict[str, object]] = []

    async def publish(_agent: str, _session: str, event: dict[str, object], **_kwargs: object) -> None:
        assert _agent == str(db.agent.id)
        assert _session == str(db.session_id)
        assert _kwargs["user_id"] == str(db.user.id)
        assert event["turn_id"] == event["message_id"]
        assert event["turn_id"] != str(native.id)
        events.append(event)

    from app.api.websocket import manager

    monkeypatch.setattr(manager, "send_to_session", publish)
    try:
        yield RecoveryCase(db, native, root, sqlite, events)
    finally:
        sqlite.close()


@pytest.mark.parametrize(
    ("status", "media_only"), [("pending", False), ("delivered", False), ("completed", False), ("completed", True)]
)
async def test_background_terminal_is_persisted_once_without_settling_native(
    recovery_case: RecoveryCase,
    status: str,
    media_only: bool,
) -> None:
    case, db = recovery_case, recovery_case.db
    if media_only:
        case.sqlite.execute("DELETE FROM transcript_events WHERE seq = 2")
        append(case.sqlite, "physical", 2, text="MEDIA:/home/node/custom-state/media/result.jpg")
    async with transaction(db.first):
        await db.first.execute(
            "UPDATE gateway_messages SET status = %(status)s WHERE id = %(id)s",
            {"status": status, "id": case.native.id},
        )
    counts: list[int] = []

    async def scan() -> None:
        counts.append(await DelayedReplyRecovery().scan_once())

    async with anyio.create_task_group() as tasks:
        tasks.start_soon(scan)
        tasks.start_soon(scan)
    assert sorted(counts) == [0, 1]
    assert await DelayedReplyRecovery().scan_once() == 0
    async with transaction(db.first):
        messages = await chat_message_dao.list_recent(
            agent_id=db.agent.id, conversation_id=str(db.session_id), limit=10
        )
        original = await db.first.fetchone(
            "SELECT result, status FROM gateway_messages WHERE id = %(id)s", {"id": case.native.id}
        )
    assert [(row.content, [ref.path for ref in row.attachments or ()]) for row in messages] == [
        ("" if media_only else "Finished", ["media/result.jpg"])
    ]
    assert original == {"result": "Original answer", "status": status}
    assert len(case.events) == 1
    assert case.events[0]["message_id"] == str(messages[0].id)


async def test_missing_media_enriches_same_message_after_cursor_advanced(recovery_case: RecoveryCase) -> None:
    case = recovery_case
    (case.root / "media/result.jpg").unlink()
    assert await DelayedReplyRecovery().scan_once() == 1
    original_id = case.events[0]["message_id"]
    assert "attachments" not in case.events[0]
    (case.root / "media/result.jpg").write_bytes(b"now ready")
    assert await DelayedReplyRecovery().scan_once() == 1
    assert await DelayedReplyRecovery().scan_once() == 0
    assert [event["message_id"] for event in case.events] == [original_id, original_id]
    assert case.events[1]["attachments"] == [{"path": "media/result.jpg", "name": "result.jpg"}]


@pytest.mark.parametrize(
    "change", ["empty_clear", "clear", "owner_roundtrip", "tenant_roundtrip", "session_roundtrip", "delete_session"]
)
async def test_revoked_epoch_never_resurrects_history(recovery_case: RecoveryCase, change: str) -> None:
    case, db = recovery_case, recovery_case.db
    async with transaction(db.first):
        match change:
            case "empty_clear":
                await chat_message_dao.delete_for_conversation(str(db.session_id))
            case "clear":
                await chat_message_dao.insert_message(
                    agent_id=db.agent.id,
                    user_id=db.user.id,
                    role="user",
                    content="hello",
                    conversation_id=str(db.session_id),
                )
                await db.first.execute(
                    "DELETE FROM chat_messages WHERE conversation_id = %(id)s", {"id": str(db.session_id)}
                )
            case "owner_roundtrip":
                other = uuid4()
                await db.first.execute("INSERT INTO users (id, display_name) VALUES (%(id)s, 'Other')", {"id": other})
                for owner in (other, db.user.id):
                    await db.first.execute(
                        "UPDATE agents SET creator_id = %(owner)s WHERE id = %(id)s",
                        {"owner": owner, "id": db.agent.id},
                    )
            case "tenant_roundtrip":
                tenant = uuid4()
                await db.first.execute(
                    "INSERT INTO tenants (id, name, slug) VALUES (%(id)s, 'Other', %(slug)s)",
                    {"id": tenant, "slug": str(tenant)},
                )
                for tenant_id in (tenant, None):
                    await db.first.execute(
                        "UPDATE agents SET tenant_id = %(tenant)s WHERE id = %(id)s",
                        {"tenant": tenant_id, "id": db.agent.id},
                    )
            case "session_roundtrip":
                for channel in ("agent", "web"):
                    await db.first.execute(
                        "UPDATE chat_sessions SET source_channel = %(channel)s WHERE id = %(id)s",
                        {"channel": channel, "id": db.session_id},
                    )
            case "delete_session":
                await db.first.execute("DELETE FROM chat_sessions WHERE id = %(id)s", {"id": db.session_id})
    assert await DelayedReplyRecovery().scan_once() == 0
    assert case.events == []
    async with transaction(db.first):
        assert (
            await chat_message_dao.list_recent(agent_id=db.agent.id, conversation_id=str(db.session_id), limit=10) == []
        )


async def test_historical_unbound_transcript_and_model_refs(recovery_case: RecoveryCase) -> None:
    case = recovery_case
    case.sqlite.execute(
        "INSERT INTO session_windows VALUES ('historical', ?)", (f"agent:main:hook:maraclaw:{case.db.session_id}",)
    )
    append(case.sqlite, "historical", 1, text="Ambiguous history")
    append(case.sqlite, "physical", 3, text="I use provider/secret-model-v3")
    assert await DelayedReplyRecovery().scan_once() == 2
    assert all("secret-model-v3" not in str(event) and "Ambiguous history" not in str(event) for event in case.events)


async def test_publication_survives_restart_after_chat_commit(
    recovery_case: RecoveryCase, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services import openclaw_delayed_replies as recovery

    case = recovery_case
    publish = recovery._publish

    async def interrupted(*_args: object) -> None:
        raise OSError("worker interrupted after commit")

    monkeypatch.setattr(recovery, "_publish", interrupted)
    assert await DelayedReplyRecovery().scan_once() == 1
    assert case.events == []
    monkeypatch.setattr(recovery, "_publish", publish)
    assert await DelayedReplyRecovery().scan_once() == 0
    assert len(case.events) == 1
    async with transaction(case.db.first):
        assert case.native.reply_binding_id is not None
        assert await openclaw_reply_dao.pending_publications(case.native.reply_binding_id) == ()


async def test_checkpoint_failure_rolls_back_receipt_and_chat(
    recovery_case: RecoveryCase, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = recovery_case
    checkpoint = openclaw_reply_dao.checkpoint

    async def failed(*_args: object) -> None:
        raise OSError("checkpoint unavailable")

    monkeypatch.setattr(openclaw_reply_dao, "checkpoint", failed)
    assert await DelayedReplyRecovery().scan_once() == 0
    assert case.events == []
    async with transaction(case.db.first):
        assert await case.db.first.fetchval("SELECT count(*) FROM openclaw_reply_receipts") == 0
        assert await case.db.first.fetchval("SELECT count(*) FROM chat_messages") == 0
    monkeypatch.setattr(openclaw_reply_dao, "checkpoint", checkpoint)
    assert await DelayedReplyRecovery().scan_once() == 1
    assert len(case.events) == 1


async def test_clear_between_chat_commit_and_publication_fences_send(
    recovery_case: RecoveryCase, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services import openclaw_delayed_replies as recovery

    case = recovery_case
    publish = recovery._publish

    async def clear_then_publish(binding, message) -> None:
        async with transaction(case.db.first):
            await chat_message_dao.delete_for_conversation(str(case.db.session_id))
        await publish(binding, message)

    monkeypatch.setattr(recovery, "_publish", clear_then_publish)
    assert await DelayedReplyRecovery().scan_once() == 1
    assert case.events == []
    assert await DelayedReplyRecovery().scan_once() == 0


async def test_page_never_splits_an_agent_with_more_than_twenty_bindings(recovery_case: RecoveryCase) -> None:
    db = recovery_case.db
    async with transaction(db.first):
        for _ in range(24):
            session = uuid4()
            await db.first.execute(
                "INSERT INTO chat_sessions (id, agent_id, user_id, title, source_channel) "
                "VALUES (%(id)s, %(agent)s, %(user)s, 'Extra', 'web')",
                {"id": session, "agent": db.agent.id, "user": db.user.id},
            )
            await db.first.execute(
                "INSERT INTO openclaw_reply_bindings (id, agent_id, session_id, owner_id, tenant_id, user_id) "
                "VALUES (%(id)s, %(agent)s, %(session)s, %(user)s, %(tenant)s, %(user)s)",
                {
                    "id": uuid4(),
                    "agent": db.agent.id,
                    "session": session,
                    "user": db.user.id,
                    "tenant": db.agent.tenant_id,
                },
            )
        page = await openclaw_reply_dao.page(None)
        assert len(page) == 25
        assert {binding.agent_id for binding in page} == {db.agent.id}
        assert await openclaw_reply_dao.page(db.agent.id) == ()
