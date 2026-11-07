import json
import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest

import test_shared_gateway_postgres as shared_fixtures
from app.dao.chat_dao import chat_message_dao
from app.dao.gateway_message_dao import gateway_message_dao
from app.db.session import transaction
from app.records.gateway_message import GatewayMessageRecord
from app.services import openclaw_dispatch_reconciler as reconciler, openclaw_gateway_delivery as delivery
from app.services.openclaw_turns import runtime_session_key
from test_chat_submission_postgres import ReceiptDatabase

receipt_database = shared_fixtures.receipt_database
pooled_database = shared_fixtures.pooled_database


@contextmanager
def guest_input(root: Path, message: GatewayMessageRecord, evidence: str) -> Generator[None]:
    path = root / "agents/main/agent/openclaw-agent.sqlite"
    path.parent.mkdir(parents=True)
    with sqlite3.connect(path) as sqlite:
        sqlite.execute("PRAGMA journal_mode=WAL")
        sqlite.execute(
            "CREATE TABLE session_pending_inputs (input_id TEXT PRIMARY KEY, session_key TEXT, session_id TEXT, "
            "idempotency_key TEXT, run_id TEXT, state TEXT, consumed_event_id TEXT, lifecycle_generation TEXT)"
        )
        sqlite.execute(
            "CREATE TABLE session_nodes (session_key TEXT PRIMARY KEY, current_session_id TEXT, entry_json TEXT)"
        )
        sqlite.execute(
            "INSERT INTO session_nodes VALUES (?, ?, ?)",
            (
                runtime_session_key(message),
                "physical-session",
                json.dumps(
                    {
                        "status": "done",
                        "restartRecoveryTerminalRunIds": [str(message.id)],
                        "restartRecoveryTerminalDeliveryEvidence": [
                            {"runId": str(message.id), "captured": True, "transcriptRunId": "recovery-run"}
                        ],
                    }
                ),
            ),
        )
        sqlite.execute(
            "INSERT INTO session_pending_inputs VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                str(uuid4()),
                runtime_session_key(message) if evidence != "foreign_session" else "agent:main:foreign",
                "physical-session",
                f"{message.id}:user",
                str(message.id),
                "queued" if evidence == "queued" else "interrupted",
                "consumed-event" if evidence == "consumed" else None,
                "previous-lifecycle",
            ),
        )
        sqlite.commit()
        yield


@pytest.mark.parametrize(
    "evidence", ["interrupted", "queued", "consumed", "foreign_session", "revoked_binding", "binding_revoked_at_write"]
)
async def test_interruption_witness_unblocks_queue_only_with_current_unconsumed_input(
    pooled_database: ReceiptDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    evidence: str,
) -> None:
    db = pooled_database
    async with transaction(db.first):
        messages = [
            await gateway_message_dao.create(
                obj_in={
                    "agent_id": db.agent.id,
                    "sender_user_id": db.user.id,
                    "conversation_id": str(db.session_id),
                    "content": content,
                    "created_at": datetime(2026, 11, 1, tzinfo=UTC) + timedelta(seconds=index),
                }
            )
            for index, content in enumerate(("interrupted predecessor", "officecli question"))
        ]
        claimed = await gateway_message_dao.claim_ordinary_head(messages[0].id, db.agent.id)
        assert claimed is not None
        assert claimed.reply_binding_id is not None

    from app.services.agent_manager import agent_manager

    root = tmp_path / str(db.agent.id)
    monkeypatch.setattr(agent_manager, "_agent_dir", lambda _agent_id: root)
    monkeypatch.setattr(reconciler, "wake_shared_lane", lambda *_args: None)
    published: list[delivery.RecordedReport] = []

    async def publish(_agent: object, report: delivery.RecordedReport) -> None:
        published.append(report)

    monkeypatch.setattr(delivery, "publish_report", publish)
    original_complete = gateway_message_dao.complete

    async def revoke_at_write(
        message_id: UUID, agent_id: UUID, *, result: str, now: datetime, binding_id: UUID | None = None
    ) -> GatewayMessageRecord | None:
        async with transaction(db.second):
            await db.second.execute(
                "DELETE FROM openclaw_reply_bindings WHERE id = %(id)s", {"id": claimed.reply_binding_id}
            )
        return await original_complete(message_id, agent_id, result=result, now=now, binding_id=binding_id)

    if evidence == "binding_revoked_at_write":
        monkeypatch.setattr(gateway_message_dao, "complete", revoke_at_write)
    with guest_input(root, claimed, evidence):
        if evidence == "revoked_binding":
            async with transaction(db.first):
                await db.first.execute(
                    "DELETE FROM openclaw_reply_bindings WHERE id = %(id)s", {"id": claimed.reply_binding_id}
                )
        await reconciler.OpenClawDispatchReconciler().scan_once()

    async with transaction(db.first):
        stored = await gateway_message_dao.get_for_agent(claimed.id, db.agent.id)
        pending, _ = await gateway_message_dao.pending_dispatch_page(50)
        chat = await chat_message_dao.list_recent(agent_id=db.agent.id, conversation_id=str(db.session_id), limit=10)
    assert stored is not None
    if evidence == "interrupted":
        assert stored.status == "completed"
        assert [row.id for row in pending] == [messages[1].id]
        assert len(chat) == 1
        assert chat[0].content == stored.result
        assert [report.message.id for report in published] == [claimed.id]
        assert await delivery.settle_native_report(db.agent, claimed.id, "late competing result") is None
    else:
        assert (stored.status, stored.result) == ("delivered", None)
        assert pending == []
        assert chat == []
        assert published == []
