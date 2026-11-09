from unittest.mock import AsyncMock
from uuid import UUID

import anyio
import pytest
from pydantic import TypeAdapter, ValidationError

from app.api import notification as api
from app.core.domain.notifications import BroadcastJobStatus
from app.dao.broadcast_job_batch import process_broadcast_batch
from app.db.errors import CheckViolationError
from app.records.broadcast_job import BroadcastJobRecord
from app.schemas.notification_responses import NotificationBroadcastJobStatusResponse
from broadcast_email_persistence_fixtures import PersistenceDB, broadcast_db, member, tenant
from broadcast_job_fixtures import PATH, accept, admin, agent, client_for
from broadcast_jobs_api_postgres import (
    test_idempotency_keys_are_scoped_by_tenant_and_sender,
    test_job_acceptance_is_committed_before_http_body,
    test_job_replay_precedes_smtp_and_conflict_preserves_exact_draft,
    test_job_requires_bounded_nonblank_key,
    test_job_routes_preserve_authentication_and_tenant_gates,
)
from broadcast_jobs_races_postgres import (
    test_batch_cancellation_rolls_back_and_restarts_without_duplicate_effects,
    test_captured_target_cannot_rejoin_during_batch,
    test_fanout_loop_resumes_after_timed_out_batch,
    test_membership_change_while_batch_waits_on_target_lock_is_skipped,
)

__all__ = [
    "broadcast_db",
    "test_batch_cancellation_rolls_back_and_restarts_without_duplicate_effects",
    "test_captured_target_cannot_rejoin_during_batch",
    "test_fanout_loop_resumes_after_timed_out_batch",
    "test_idempotency_keys_are_scoped_by_tenant_and_sender",
    "test_job_acceptance_is_committed_before_http_body",
    "test_job_replay_precedes_smtp_and_conflict_preserves_exact_draft",
    "test_job_requires_bounded_nonblank_key",
    "test_job_routes_preserve_authentication_and_tenant_gates",
    "test_membership_change_while_batch_waits_on_target_lock_is_skipped",
]


async def test_fixed_ledger_resumes_bounded_batches_after_restart_and_excludes_late_members(
    broadcast_db: PersistenceDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    sender = await admin(broadcast_db, 3)
    assert sender.tenant_id is not None
    captured_agent = await agent(broadcast_db, sender)
    monkeypatch.setattr("app.services.system_email_service.resolve_email_config_async", AsyncMock(return_value=True))
    receipt = await accept(sender, email=True)
    assert (receipt["users_total"], receipt["agents_total"]) == (3, 1)
    pending = await api.broadcast_job_status(receipt["broadcast_id"], sender)
    assert pending["status"] is BroadcastJobStatus.PENDING
    selected = await broadcast_db.admin.fetchall(
        "SELECT target_id, to_email FROM broadcast_job_recipients WHERE kind = 'user'"
    )
    await broadcast_db.admin.execute("UPDATE identities SET email = 'changed-' || email")
    late_user = await member(broadcast_db, sender.tenant_id)
    late_agent = await agent(broadcast_db, sender)

    assert await process_broadcast_batch(limit=2)
    progressing = await api.broadcast_job_status(receipt["broadcast_id"], sender)
    assert progressing["status"] is BroadcastJobStatus.PROCESSING
    assert progressing["processed"] == 2
    assert await broadcast_db.admin.fetchval("SELECT count(*) FROM broadcast_history") == 0
    await broadcast_db.restart()
    assert await process_broadcast_batch(limit=2)
    assert not await process_broadcast_batch(limit=2)
    status = await api.broadcast_job_status(receipt["broadcast_id"], sender)
    assert status["status"] is BroadcastJobStatus.COMPLETED
    assert (
        status["users_notified"],
        status["agents_notified"],
        status["emails_queued"],
        status["skipped"],
    ) == (3, 1, 3, 0)
    targets = await broadcast_db.admin.fetchall("SELECT user_id, agent_id FROM notifications")
    assert {row["user_id"] for row in targets if row["user_id"]} == {row["target_id"] for row in selected}
    assert {row["agent_id"] for row in targets if row["agent_id"]} == {captured_agent}
    assert late_user.id not in {row["user_id"] for row in targets}
    assert late_agent not in {row["agent_id"] for row in targets}
    assert {row["to_email"] for row in await broadcast_db.admin.fetchall("SELECT to_email FROM broadcast_emails")} == {
        row["to_email"] for row in selected
    }
    monkeypatch.setattr("app.services.system_email_service.resolve_email_config_async", AsyncMock(return_value=None))
    assert await accept(sender, email=True) == receipt
    assert await broadcast_db.admin.fetchone(
        "SELECT users_notified, agents_notified, emails_sent FROM broadcast_history"
    ) == {"users_notified": 3, "agents_notified": 1, "emails_sent": 3}
    async with client_for(sender) as client:
        response = await client.get(f"{PATH}/{receipt['broadcast_id']}")
    assert response.status_code == 200
    assert response.json()["status"] == "completed"
    assert response.json()["completed_at"] is not None


async def test_batch_failure_rolls_back_effects_cursor_and_history(
    broadcast_db: PersistenceDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    sender = await admin(broadcast_db, 2)
    monkeypatch.setattr("app.services.system_email_service.resolve_email_config_async", AsyncMock(return_value=True))
    receipt = await accept(sender, email=True)
    await broadcast_db.admin.execute("ALTER TABLE broadcast_history ADD CONSTRAINT reject_history CHECK (false)")
    with pytest.raises(CheckViolationError):
        _ = await process_broadcast_batch(limit=2)
    status = await api.broadcast_job_status(receipt["broadcast_id"], sender)
    assert (status["processed"], status["users_notified"], status["status"]) == (0, 0, BroadcastJobStatus.PENDING)
    assert await broadcast_db.admin.fetchval("SELECT count(*) FROM notifications") == 0
    assert await broadcast_db.admin.fetchval("SELECT count(*) FROM broadcast_emails") == 0
    await broadcast_db.admin.execute("ALTER TABLE broadcast_history DROP CONSTRAINT reject_history")
    assert await process_broadcast_batch(limit=2)
    assert await broadcast_db.admin.fetchval("SELECT count(*) FROM notifications") == 2


async def test_concurrent_acceptance_and_workers_produce_one_job_and_unique_effects(
    broadcast_db: PersistenceDB,
) -> None:
    sender = await admin(broadcast_db, 7)
    receipts: list[UUID] = []

    async def submit() -> None:
        receipts.append((await accept(sender))["broadcast_id"])

    async with anyio.create_task_group() as tasks:
        tasks.start_soon(submit)
        tasks.start_soon(submit)
    assert receipts[0] == receipts[1]
    successful_batches = 0

    async def process() -> None:
        nonlocal successful_batches
        while successful_batches < 4:
            if await process_broadcast_batch(limit=2):
                successful_batches += 1

    with anyio.fail_after(10):
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(process)
            tasks.start_soon(process)
    assert successful_batches == 4
    assert await broadcast_db.admin.fetchone(
        "SELECT (SELECT count(*) FROM notifications) AS notifications, "
        "(SELECT count(*) FROM broadcast_history) AS history, (SELECT cursor FROM broadcast_jobs) AS cursor"
    ) == {"notifications": 7, "history": 1, "cursor": 7}


@pytest.mark.parametrize("mutation", ["delete", "inactive", "move"])
async def test_captured_ineligible_targets_skip_without_losing_cursor_positions(
    broadcast_db: PersistenceDB, mutation: str
) -> None:
    sender = await admin(broadcast_db, 3)
    captured_agent = await agent(broadcast_db, sender)
    receipt = await accept(sender)
    ids = await broadcast_db.admin.fetchall(
        "SELECT target_id FROM broadcast_job_recipients WHERE kind = 'user' ORDER BY ordinal"
    )
    target = ids[0]["target_id"]
    match mutation:
        case "delete":
            await broadcast_db.admin.execute("DELETE FROM users WHERE id = %(id)s", {"id": target})
            await broadcast_db.admin.execute("DELETE FROM agents WHERE id = %(id)s", {"id": captured_agent})
        case "inactive":
            await broadcast_db.admin.execute("UPDATE users SET is_active = false WHERE id = %(id)s", {"id": target})
        case "move":
            destination = await tenant(broadcast_db)
            await broadcast_db.admin.execute(
                "UPDATE users SET tenant_id = %(tenant)s WHERE id = %(id)s", {"id": target, "tenant": destination}
            )
            await broadcast_db.admin.execute(
                "UPDATE agents SET tenant_id = %(tenant)s WHERE id = %(id)s",
                {"id": captured_agent, "tenant": destination},
            )
    for _ in range(4):
        assert await process_broadcast_batch(limit=1)
    status = await api.broadcast_job_status(receipt["broadcast_id"], sender)
    skipped = 1 if mutation == "inactive" else 2
    assert (status["processed"], status["skipped"], status["users_notified"], status["agents_notified"]) == (
        4,
        skipped,
        2,
        2 - skipped,
    )
    assert status["status"] is BroadcastJobStatus.COMPLETED
    assert not await process_broadcast_batch(limit=1)


async def test_sender_deletion_preserves_accepted_work_and_tenant_teardown_removes_ledger(
    broadcast_db: PersistenceDB,
) -> None:
    sender = await admin(broadcast_db, 1)
    receipt = await accept(sender)
    await broadcast_db.admin.execute("DELETE FROM users WHERE id = %(id)s", {"id": sender.id})
    assert await process_broadcast_batch(limit=1)
    assert await broadcast_db.admin.fetchone("SELECT sender_id, sender_name FROM broadcast_history") == {
        "sender_id": None,
        "sender_name": "Alice",
    }
    await broadcast_db.admin.execute("DELETE FROM notifications")
    await broadcast_db.admin.execute("DELETE FROM users WHERE tenant_id = %(id)s", {"id": sender.tenant_id})
    await broadcast_db.admin.execute("DELETE FROM tenants WHERE id = %(id)s", {"id": sender.tenant_id})
    assert (
        await broadcast_db.admin.fetchval(
            "SELECT count(*) FROM broadcast_jobs WHERE broadcast_id = %(id)s", {"id": receipt["broadcast_id"]}
        )
        == 0
    )
    assert await broadcast_db.admin.fetchval("SELECT count(*) FROM broadcast_job_recipients") == 0


async def test_empty_audience_completes_once_and_new_operation_keys_are_separate(broadcast_db: PersistenceDB) -> None:
    sender = await admin(broadcast_db)
    receipt = await accept(sender)
    assert (receipt["users_total"], receipt["agents_total"]) == (0, 0)
    assert await process_broadcast_batch(limit=2)
    assert not await process_broadcast_batch(limit=2)
    status = await api.broadcast_job_status(receipt["broadcast_id"], sender)
    assert (status["status"], status["processed"], status["skipped"]) == (BroadcastJobStatus.COMPLETED, 0, 0)
    legacy = await api.broadcast_notification(api.BroadcastRequest(title="Legacy", body=""), sender, "draft-1")
    assert legacy["broadcast_id"] != receipt["broadcast_id"]
    assert await broadcast_db.admin.fetchval("SELECT count(*) FROM broadcast_history") == 2


@pytest.mark.parametrize(
    ("raw_status", "expected"),
    [
        ("pending", BroadcastJobStatus.PENDING),
        ("processing", BroadcastJobStatus.PROCESSING),
        ("completed", BroadcastJobStatus.COMPLETED),
    ],
)
async def test_job_status_decoders_preserve_members_and_json_values(
    broadcast_db: PersistenceDB, raw_status: str, expected: BroadcastJobStatus
) -> None:
    sender = await admin(broadcast_db)
    receipt = await accept(sender)
    row = await broadcast_db.admin.fetchone("SELECT * FROM broadcast_jobs")
    assert row is not None
    response = await api.broadcast_job_status(receipt["broadcast_id"], sender)
    adapter = TypeAdapter(NotificationBroadcastJobStatusResponse)

    record = BroadcastJobRecord.from_row({**row, "status": raw_status})
    decoded = adapter.validate_python({**response, "status": raw_status})

    assert record.status is expected
    assert decoded["status"] is expected
    assert adapter.dump_python(decoded, mode="json")["status"] == raw_status


@pytest.mark.parametrize("raw_status", ["accepted", "invalid", None])
async def test_job_status_decoders_reject_values_outside_the_fanout_lifecycle(
    broadcast_db: PersistenceDB, raw_status: str | None
) -> None:
    sender = await admin(broadcast_db)
    receipt = await accept(sender)
    row = await broadcast_db.admin.fetchone("SELECT * FROM broadcast_jobs")
    assert row is not None
    response = await api.broadcast_job_status(receipt["broadcast_id"], sender)

    with pytest.raises(ValidationError):
        _ = BroadcastJobRecord.from_row({**row, "status": raw_status})
    with pytest.raises(ValidationError):
        _ = TypeAdapter(NotificationBroadcastJobStatusResponse).validate_python({**response, "status": raw_status})
