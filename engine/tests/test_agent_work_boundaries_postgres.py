from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import HTTPException
from tests.channel_submission_fixtures import ChannelDatabase, channel_db, receipt_db
from tests.test_channel_event_receipts_postgres import counts

from app.core.json_types import uuid_from_row
from app.dao.agent_dao import agent_dao
from app.db.session import transaction
from app.services import openclaw_gateway_delivery as delivery, openclaw_routing as routing
from app.services.channels.submissions import accept_channel_event
from app.services.quota_guard import AgentExpired

__all__ = ["channel_db", "receipt_db"]


async def disable_owner(fixture: ChannelDatabase) -> None:
    async with transaction(fixture.db.second):
        await fixture.db.second.execute("UPDATE users SET is_active=FALSE WHERE id=%(id)s", {"id": fixture.db.user.id})


@pytest.mark.parametrize("state", ["owner", "identity", "tenant", "expired"])
async def test_new_denial_has_no_receipt_quota_session_transcript_or_queue(channel_db: ChannelDatabase, state):
    fixture = channel_db
    async with transaction(fixture.db.second):
        match state:
            case "owner":
                await fixture.db.second.execute(
                    "UPDATE users SET is_active=FALSE WHERE id=%(id)s", {"id": fixture.db.user.id}
                )
            case "identity":
                identity_id = uuid4()
                await fixture.db.second.execute(
                    "INSERT INTO identities(id,is_active) VALUES (%(id)s,FALSE)", {"id": identity_id}
                )
                await fixture.db.second.execute(
                    "UPDATE users SET identity_id=%(identity)s WHERE id=%(id)s",
                    {"identity": identity_id, "id": fixture.db.user.id},
                )
            case "tenant":
                await fixture.db.second.execute("UPDATE tenants SET is_active=FALSE")
            case "expired":
                await fixture.db.second.execute("UPDATE agents SET expires_at=now(),is_expired=FALSE")
    with pytest.raises(AgentExpired if state == "expired" else HTTPException):
        async with transaction(fixture.db.first):
            await accept_channel_event(fixture.event)
    assert await counts(fixture) == {
        "receipts": 0,
        "messages": 0,
        "enqueued": 0,
        "intents": 0,
        "charged": 0,
        "creator_charged": 0,
    }
    async with transaction(fixture.db.first):
        assert await fixture.db.first.fetchval("SELECT count(*) FROM chat_sessions") == 1


async def test_owner_disabled_committed_replay_does_not_charge_or_enqueue(channel_db: ChannelDatabase):
    fixture = channel_db
    async with transaction(fixture.db.first):
        await accept_channel_event(fixture.event)
    before = await counts(fixture)
    await disable_owner(fixture)
    async with transaction(fixture.db.first):
        replay = await accept_channel_event(fixture.event)
    assert replay.replayed
    assert await counts(fixture) == before


async def test_stopped_agent_still_accepts_a_new_channel_event(channel_db: ChannelDatabase):
    fixture = channel_db
    async with transaction(fixture.db.second):
        await fixture.db.second.execute("UPDATE agents SET status='stopped'")
    async with transaction(fixture.db.first):
        accepted = await accept_channel_event(fixture.event)
    assert not accepted.replayed
    assert (await counts(fixture))["enqueued"] == 1


@pytest.mark.parametrize("boundary", ["poll", "wake", "prepare", "enqueue"])
async def test_accepted_queue_cannot_bypass_later_owner_disable(channel_db: ChannelDatabase, monkeypatch, boundary):
    fixture = channel_db
    async with transaction(fixture.db.first):
        await accept_channel_event(fixture.event)
        stale_agent = await agent_dao.get(fixture.db.agent.id)
    assert stale_agent is not None
    await disable_owner(fixture)
    wake = AsyncMock(return_value=True)
    ensure = AsyncMock()
    monkeypatch.setattr("app.services.openclaw_inbox.wake_openclaw_inbox", wake)
    monkeypatch.setattr(routing, "ensure_agent_company_models", ensure)
    async with transaction(fixture.db.first):
        message = await fixture.db.first.fetchone("SELECT id FROM gateway_messages")
        assert message is not None
        match boundary:
            case "poll":
                with pytest.raises(HTTPException):
                    await delivery.claim_poll_batch(stale_agent.id)
            case "wake":
                await routing._run_inbox_wake(stale_agent, "queued", uuid_from_row(message["id"]))
            case "prepare":
                with pytest.raises(HTTPException):
                    await routing.prepare_openclaw_message(agent=stale_agent, content="new work")
            case "enqueue":
                prepared = routing.PreparedOpenClawMessage(stale_agent, "prepared", "primary", None, None, "test")
                with pytest.raises(HTTPException):
                    await routing.enqueue_prepared_openclaw_message(prepared)
        assert await fixture.db.first.fetchval("SELECT status FROM gateway_messages") == "pending"
    wake.assert_not_awaited()
    ensure.assert_not_awaited()
