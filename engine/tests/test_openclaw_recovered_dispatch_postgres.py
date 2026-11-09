"""Successful restart recovery crosses real SQLite and PostgreSQL boundaries."""

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid5

import anyio
import pytest

import test_openclaw_delayed_replies_postgres as delayed_fixtures
from app.dao.chat_dao import chat_message_dao
from app.dao.gateway_message_dao import gateway_message_dao
from app.db.session import transaction
from app.records.gateway_message import GatewayMessageRecord
from app.services import openclaw_dispatch_reconciler as reconciler, openclaw_gateway_delivery as delivery
from app.services.openclaw_delayed_replies import DelayedReplyRecovery
from app.services.openclaw_turns import runtime_session_key
from test_openclaw_delayed_replies_postgres import RecoveryCase

receipt_database = delayed_fixtures.receipt_database
pooled_database = delayed_fixtures.pooled_database
recovery_case = delayed_fixtures.recovery_case


@dataclass(frozen=True, slots=True)
class RecoveredDispatch:
    case: RecoveryCase
    successor: GatewayMessageRecord
    reports: list[delivery.RecordedReport]


@pytest.fixture
async def recovered_dispatch(recovery_case: RecoveryCase, monkeypatch: pytest.MonkeyPatch) -> RecoveredDispatch:
    case, db = recovery_case, recovery_case.db
    async with transaction(db.first):
        await db.first.execute(
            "UPDATE gateway_messages SET status = 'delivered', result = NULL WHERE id = %(id)s",
            {"id": case.native.id},
        )
        successor = await gateway_message_dao.create(
            obj_in={
                "agent_id": db.agent.id,
                "sender_user_id": db.user.id,
                "conversation_id": str(db.session_id),
                "content": "successor must become eligible",
            }
        )
    case.sqlite.execute("CREATE TABLE session_nodes (session_key TEXT PRIMARY KEY, entry_json TEXT)")
    case.sqlite.execute(
        "INSERT INTO session_nodes VALUES (?, ?)",
        (
            runtime_session_key(case.native),
            json.dumps(
                {
                    "status": "done",
                    "restartRecoveryTerminalRunIds": [str(case.native.id), "tool:background:ok"],
                    "restartRecoveryTerminalDeliveryEvidence": [
                        {
                            "runId": str(case.native.id),
                            "captured": True,
                            "payloads": [{"visible": True}],
                            "transcriptRunId": "tool:background:ok",
                        }
                    ],
                    "restartRecoveryDeliverySourceRunId": None,
                    "restartRecoveryDeliveryRunId": None,
                    "restartRecoveryRunId": None,
                }
            ),
        ),
    )
    case.sqlite.commit()
    reports: list[delivery.RecordedReport] = []

    async def publish(_agent, report: delivery.RecordedReport) -> None:
        reports.append(report)

    monkeypatch.setattr(delivery, "publish_report", publish)
    monkeypatch.setattr(reconciler, "wake_shared_lane", lambda *_args: None)
    return RecoveredDispatch(case, successor, reports)


async def test_published_recovery_settles_delivered_head_without_replay_or_duplicate_chat(
    recovered_dispatch: RecoveredDispatch,
) -> None:
    fixture = recovered_dispatch
    case, db = fixture.case, fixture.case.db
    assert await DelayedReplyRecovery().scan_once() == 1
    async with transaction(db.first):
        pending, _ = await gateway_message_dao.pending_dispatch_page(50)
        assert pending == []
        assert await db.first.fetchval("SELECT cursor FROM openclaw_reply_bindings") == {"physical": 2}

    assert await reconciler.OpenClawDispatchReconciler().scan_once() == 1

    async with transaction(db.first):
        stored = await gateway_message_dao.get_for_agent(case.native.id, db.agent.id)
        pending, _ = await gateway_message_dao.pending_dispatch_page(50)
        chat = await chat_message_dao.list_recent(agent_id=db.agent.id, conversation_id=str(db.session_id), limit=10)
        receipts = await db.first.fetchval("SELECT count(*) FROM openclaw_reply_receipts")
        cursor = await db.first.fetchval("SELECT cursor FROM openclaw_reply_bindings")
    assert stored is not None
    assert (stored.status, stored.result) == ("completed", "Finished")
    assert stored.delivered_at == case.native.delivered_at
    assert stored.completed_at is not None
    assert [row.id for row in pending] == [fixture.successor.id]
    assert len(chat) == receipts == 1
    assert case.native.reply_binding_id is not None
    assert chat[0].id == uuid5(case.native.reply_binding_id, "event-physical-2")
    assert [ref.path for ref in chat[0].attachments or ()] == ["media/result.jpg"]
    assert cursor == {"physical": 2}
    assert len(case.events) == 1
    assert len(fixture.reports) == 1
    assert fixture.reports[0].message.id == case.native.id
    assert fixture.reports[0].assistant_message_id == chat[0].id
    assert await delivery.settle_native_report(db.agent, case.native.id, "late duplicate report") is None


@pytest.mark.parametrize("waiting", ["receipt", "publication", "media"])
async def test_native_waits_for_independent_recovery_to_finish_persistence_and_publication(
    recovered_dispatch: RecoveredDispatch, monkeypatch: pytest.MonkeyPatch, waiting: str
) -> None:
    from app.services import openclaw_delayed_replies as delayed

    case, db = recovered_dispatch.case, recovered_dispatch.case.db
    publish = delayed._publish

    async def crash_before_publication(*_args) -> None:
        raise OSError("worker stopped after chat commit")

    if waiting == "media":
        (case.root / "media/result.jpg").unlink()
    if waiting == "publication":
        monkeypatch.setattr(delayed, "_publish", crash_before_publication)
    if waiting != "receipt":
        assert await DelayedReplyRecovery().scan_once() == 1
    scanner = reconciler.OpenClawDispatchReconciler()
    assert await scanner.scan_once() == 0
    async with transaction(db.first):
        stored = await gateway_message_dao.get_for_agent(case.native.id, db.agent.id)
        assert stored is not None
        assert stored.status == "delivered"
    monkeypatch.setattr(delayed, "_publish", publish)
    if waiting == "media":
        (case.root / "media/result.jpg").write_bytes(b"ready")
    await DelayedReplyRecovery().scan_once()

    assert await scanner.scan_once() == 1

    async with transaction(db.first):
        stored = await gateway_message_dao.get_for_agent(case.native.id, db.agent.id)
        assert stored is not None
        assert (stored.status, stored.result) == ("completed", "Finished")
        assert await db.first.fetchval("SELECT count(*) FROM chat_messages") == 1


async def test_two_recovery_scans_adopt_only_one_report(recovered_dispatch: RecoveredDispatch) -> None:
    case, db = recovered_dispatch.case, recovered_dispatch.case.db
    assert await DelayedReplyRecovery().scan_once() == 1

    async with anyio.create_task_group() as tasks:
        tasks.start_soon(reconciler.OpenClawDispatchReconciler().scan_once)
        tasks.start_soon(reconciler.OpenClawDispatchReconciler().scan_once)

    async with transaction(db.first):
        stored = await gateway_message_dao.get_for_agent(case.native.id, db.agent.id)
        assert stored is not None
        assert (stored.status, stored.result) == ("completed", "Finished")
        assert await db.first.fetchval("SELECT count(*) FROM chat_messages") == 1
        assert await db.first.fetchval("SELECT count(*) FROM openclaw_reply_receipts") == 1
    assert len(case.events) == 1
    assert len(recovered_dispatch.reports) == 1


@pytest.mark.parametrize("winner", ["native", "recovery", "race"])
async def test_competing_native_completion_keeps_first_report(
    recovered_dispatch: RecoveredDispatch, winner: str
) -> None:
    case, db = recovered_dispatch.case, recovered_dispatch.case.db
    assert await DelayedReplyRecovery().scan_once() == 1
    from app.services.openclaw_recovered_dispatch import settle_recovered_report

    if winner == "native":
        first = await delivery.settle_native_report(db.agent, case.native.id, "Native answer")
        second = await settle_recovered_report(db.agent, case.native)
    elif winner == "recovery":
        first = await settle_recovered_report(db.agent, case.native)
        second = await delivery.settle_native_report(db.agent, case.native.id, "Native answer")
    else:
        reports: list[delivery.RecordedReport | None] = []

        async def native() -> None:
            reports.append(await delivery.settle_native_report(db.agent, case.native.id, "Native answer"))

        async def recovery() -> None:
            reports.append(await settle_recovered_report(db.agent, case.native))

        async with anyio.create_task_group() as tasks:
            tasks.start_soon(native)
            tasks.start_soon(recovery)
        winners = [report for report in reports if report is not None]
        assert len(winners) == 1
        first, second = winners[0], None
    assert first is not None
    assert second is None
    async with transaction(db.first):
        stored = await gateway_message_dao.get_for_agent(case.native.id, db.agent.id)
        assert stored is not None
        assert stored.result == first.message.result
        assert await db.first.fetchval("SELECT count(*) FROM chat_messages") == (
            2 if stored.result == "Native answer" else 1
        )


async def test_recovery_completion_dao_fences_exact_captured_claim(recovered_dispatch: RecoveredDispatch) -> None:
    case, db = recovered_dispatch.case, recovered_dispatch.case.db
    assert await DelayedReplyRecovery().scan_once() == 1
    async with transaction(db.first):
        await db.first.execute(
            "UPDATE gateway_messages SET delivered_at = delivered_at + interval '1 second' WHERE id = %(id)s",
            {"id": case.native.id},
        )

        assert await gateway_message_dao.complete_recovered(case.native, "stale", datetime.now(UTC)) is None

        stored = await gateway_message_dao.get_for_agent(case.native.id, db.agent.id)
        assert stored is not None
        assert stored.status == "delivered"
        assert stored.result is None
        assert case.native.delivered_at is not None
        assert stored.delivered_at == case.native.delivered_at + timedelta(seconds=1)
