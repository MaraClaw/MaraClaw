"""Workspace disk refusal at every message-accepting site against live PostgreSQL, in the accepting transaction."""

import asyncio
from dataclasses import replace
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from tests.channel_submission_fixtures import ChannelDatabase, channel_db
from tests.personal_burst_fixtures import LinkedBurst, linked, scalar, submit
from tests.test_chat_submission_postgres import ReceiptDatabase, receipt_db, writable_receipt_db

from app.db.session import transaction
from app.records.agent import AgentRecord
from app.records.organization_plan import OrganizationPlanId
from app.services import agent_workspace_quota as quota
from app.services.agent_workspace_quota import WorkspaceDiskQuotaExceeded
from app.services.channels.personal_burst_outcome import PersonalAccepted, PersonalRejected
from app.services.channels.submissions import accept_channel_event
from app.services.chat_submissions import ChatSubmission, accept_submission
from tier_limit_fixtures import set_tenant_tier

__all__ = ["channel_db", "linked", "receipt_db", "writable_receipt_db"]

GIB = 1024**3
PRO_LIMIT = 3_221_225_472


async def _govern(db: ReceiptDatabase) -> UUID:
    """Put the fixture agent in a Pro organization."""
    tenant_id = uuid4()
    async with transaction(db.first):
        await db.first.execute(
            "INSERT INTO tenants (id, name, slug, tier) VALUES (%(id)s, 'Disk QA', %(slug)s, 'ultra')",
            {"id": tenant_id, "slug": f"disk-{tenant_id.hex[:8]}"},
        )
        await db.first.execute("UPDATE users SET tenant_id = %(tenant)s", {"tenant": tenant_id})
        await db.first.execute(
            "UPDATE agents SET tenant_id = %(tenant)s WHERE id = %(id)s", {"tenant": tenant_id, "id": db.agent.id}
        )
        await set_tenant_tier(db.first, tenant_id, OrganizationPlanId.PRO)
    return tenant_id


async def _block(db: ReceiptDatabase) -> None:
    async with transaction(db.first):
        await db.first.execute(
            "INSERT INTO agent_workspace_usage (agent_id, dir_bytes, layer_bytes, limit_bytes, measured_at, over_since) "
            "VALUES (%(id)s, %(used)s, 0, %(limit)s, now(), now())",
            {"id": db.agent.id, "used": 4 * GIB, "limit": PRO_LIMIT},
        )


async def _lift(db: ReceiptDatabase) -> None:
    async with transaction(db.first):
        await db.first.execute("DELETE FROM agent_workspace_usage WHERE agent_id = %(id)s", {"id": db.agent.id})


async def _leftovers(db: ReceiptDatabase) -> dict[str, object] | None:
    async with transaction(db.first):
        return await db.first.fetchone(
            "SELECT (SELECT count(*) FROM chat_messages) AS messages, (SELECT count(*) FROM gateway_messages) AS queued, "
            "(SELECT count(*) FROM agent_daily_message_usage) AS counted"
        )


async def _submit(db: ReceiptDatabase, tenant_id: UUID) -> None:
    async with transaction(db.first):
        _ = await accept_submission(
            ChatSubmission(client_submission_id=uuid4(), content="please continue"),
            user=replace(db.user, tenant_id=tenant_id),
            agent=replace(db.agent, tenant_id=tenant_id),
            session_id=db.session_id,
        )


async def _usage(db: ReceiptDatabase) -> dict[str, object] | None:
    async with transaction(db.first):
        return await db.first.fetchone(
            "SELECT measured_at, over_since, walk_failed_at, remeasure_requested_at FROM agent_workspace_usage "
            "WHERE agent_id = %(id)s",
            {"id": db.agent.id},
        )


def _times_out(monkeypatch: pytest.MonkeyPatch) -> None:
    def walk(_path: Path) -> int:
        raise quota.WorkspaceMeasurementTimeoutError("deadline")

    monkeypatch.setattr(quota, "measure_directory_bytes", walk)


async def test_a_blocked_agent_refuses_a_web_message_and_rolls_the_whole_turn_back(
    writable_receipt_db: ReceiptDatabase,
):
    db = writable_receipt_db
    tenant_id = await _govern(db)
    await _block(db)

    async def submit_message() -> None:
        async with transaction(db.first):
            _ = await accept_submission(
                ChatSubmission(client_submission_id=uuid4(), content="please continue"),
                user=replace(db.user, tenant_id=tenant_id),
                agent=replace(db.agent, tenant_id=tenant_id),
                session_id=db.session_id,
            )

    with pytest.raises(WorkspaceDiskQuotaExceeded) as refused:
        await submit_message()

    assert refused.value.quota_type == "agent_workspace_disk"
    assert await _leftovers(db) == {"messages": 0, "queued": 0, "counted": 0}

    await _lift(db)
    await submit_message()
    assert await _leftovers(db) == {"messages": 1, "queued": 1, "counted": 1}


async def test_a_blocked_agent_refuses_a_channel_event_and_the_redelivery_is_judged_afresh(
    channel_db: ChannelDatabase,
):
    db = channel_db.db
    async with transaction(db.first):
        await set_tenant_tier(db.first, channel_db.tenant_id, OrganizationPlanId.PRO)
    await _block(db)

    with pytest.raises(WorkspaceDiskQuotaExceeded):
        async with transaction(db.first):
            _ = await accept_channel_event(channel_db.event)

    async with transaction(db.first):
        receipts = await db.first.fetchval("SELECT count(*) FROM channel_event_receipts")
    assert receipts == 0

    await _lift(db)
    async with transaction(db.first):
        accepted = await accept_channel_event(channel_db.event)
    assert accepted.replayed is False


async def test_a_blocked_agent_turns_a_personal_original_into_a_quota_rejection_that_charges_nobody(
    linked: LinkedBurst,
):
    db = linked.f.db
    async with transaction(db.first):
        await set_tenant_tier(db.first, linked.f.tenant_id, OrganizationPlanId.PRO)
    await _block(db)

    rejected = await submit(linked, "n1", "while blocked")

    assert isinstance(rejected.personal, PersonalRejected)
    assert rejected.personal.outcome == "quota_rejected"
    assert await scalar(linked, "SELECT count(*) FROM chat_messages WHERE role = 'user'") == 0
    assert await scalar(linked, "SELECT count(*) FROM agent_daily_message_usage") == 0

    await _lift(db)
    admitted = await submit(linked, "n2", "after the delete")

    assert isinstance(admitted.personal, PersonalAccepted)
    assert await scalar(linked, "SELECT count(*) FROM agent_daily_message_usage") == 1


async def test_a_failed_walk_neither_lifts_a_block_nor_blocks_an_agent_that_was_never_measured(
    writable_receipt_db: ReceiptDatabase, monkeypatch: pytest.MonkeyPatch
):
    db = writable_receipt_db
    tenant_id = await _govern(db)
    agent = replace(db.agent, tenant_id=tenant_id)
    _times_out(monkeypatch)
    await _block(db)

    async with transaction(db.first):
        await quota.measure_agent(agent)

    with pytest.raises(WorkspaceDiskQuotaExceeded):
        await _submit(db, tenant_id)
    assert await _leftovers(db) == {"messages": 0, "queued": 0, "counted": 0}
    row = await _usage(db)
    assert row is not None
    assert (row["over_since"] is not None, row["walk_failed_at"] is not None) == (True, True)

    await _lift(db)
    async with transaction(db.first):
        await quota.measure_agent(agent)

    row = await _usage(db)
    assert row is not None
    assert (row["measured_at"], row["over_since"], row["walk_failed_at"] is not None) == (None, None, True)
    await _submit(db, tenant_id)
    assert await _leftovers(db) == {"messages": 1, "queued": 1, "counted": 1}


async def test_a_delete_that_outlasts_its_wait_keeps_the_block_for_the_worker_whose_measurement_lifts_it(
    writable_receipt_db: ReceiptDatabase, monkeypatch: pytest.MonkeyPatch
):
    # Given a blocked agent whose delete cannot get its re-measure done while the request waits.
    db = writable_receipt_db
    tenant_id = await _govern(db)
    agent = replace(db.agent, tenant_id=tenant_id)
    await _block(db)
    measure = quota.measure_agent

    async def never_finishes(_agent: AgentRecord, layers: object = None) -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(quota, "REQUEST_WAIT_SECONDS", 0.0)
    monkeypatch.setattr(quota, "measure_agent", never_finishes)
    # When the delete's refresh returns.
    async with transaction(db.first):
        await quota.refresh_workspace_usage_after_delete(agent)
    # Then the block stands and the agent waits for the worker.
    row = await _usage(db)
    assert row is not None
    assert (row["over_since"] is not None, row["remeasure_requested_at"] is not None) == (True, True)
    with pytest.raises(WorkspaceDiskQuotaExceeded):
        await _submit(db, tenant_id)
    # And the worker's measurement, which finds the workspace under its allowance, lifts the block and the ask.
    monkeypatch.setattr(quota, "measure_directory_bytes", lambda _path: GIB)
    async with transaction(db.first):
        await measure(agent)
    row = await _usage(db)
    assert row is not None
    assert (row["over_since"], row["remeasure_requested_at"]) == (None, None)
    await _submit(db, tenant_id)
