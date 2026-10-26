from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from tests.channel_submission_fixtures import channel_db, latest, receipt_db, scope_for, whatsapp_relay_db
from tests.test_channel_delivery_transport import transport

from app.dao.channel_event_receipt_dao import channel_event_receipt_dao as receipts
from app.dao.gateway_report_delivery_dao import gateway_report_delivery_dao as deliveries
from app.db.errors import CheckViolationError
from app.db.session import transaction
from app.services import gateway_report_relay as relay
from app.services.channels.submissions import accept_channel_event

__all__ = ["channel_db", "receipt_db", "whatsapp_relay_db"]


async def test_latest_evidence_uses_provider_order_not_arrival_order(channel_db):
    fixture = channel_db
    newest = fixture.event.provider_timestamp
    async with transaction(fixture.db.first):
        await accept_channel_event(fixture.event)
        await accept_channel_event(
            replace(fixture.event, event_key="older", provider_timestamp=newest - timedelta(hours=2))
        )
    assert await latest(fixture) == newest


@pytest.mark.parametrize("field", ["config_id", "agent_id", "tenant_id", "provider", "account_id", "conversation_id"])
async def test_evidence_lookup_is_exactly_scoped(channel_db, field):
    fixture = channel_db
    async with transaction(fixture.db.first):
        await accept_channel_event(fixture.event)
        scope = scope_for(fixture)
        changed = {
            "config_id": uuid4(),
            "agent_id": uuid4(),
            "tenant_id": uuid4(),
            "provider": "slack",
            "account_id": "other",
            "conversation_id": "whatsapp_other",
        }
        assert await receipts.latest_eligible_timestamp(replace(scope, **{field: changed[field]})) is None


@pytest.mark.parametrize("change", ["account", "tenant", "disabled"])
async def test_lookup_requires_current_agent_tenant_and_config_account(channel_db, change):
    fixture = channel_db
    async with transaction(fixture.db.first):
        await accept_channel_event(fixture.event)
        if change == "account":
            await fixture.db.first.execute("UPDATE channel_configs SET app_id = 'other-account'")
        elif change == "disabled":
            await fixture.db.first.execute("UPDATE channel_configs SET is_configured = FALSE")
        else:
            tenant = uuid4()
            await fixture.db.first.execute(
                "INSERT INTO tenants (id, name, slug) VALUES (%(id)s, 'new', 'new')", {"id": tenant}
            )
            await fixture.db.first.execute("UPDATE agents SET tenant_id = %(id)s", {"id": tenant})
    assert await latest(fixture) is None


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE channel_event_receipts SET provider_timestamp = clock_timestamp()",
        "UPDATE channel_event_receipts SET provider_account_id = 'changed'",
        "UPDATE channel_event_receipts SET conversation_id = 'changed'",
        "UPDATE channel_event_receipts SET created_at = clock_timestamp()",
    ],
)
async def test_receipt_evidence_is_database_immutable(channel_db, statement):
    fixture = channel_db
    async with transaction(fixture.db.first):
        await accept_channel_event(fixture.event)
    with pytest.raises(CheckViolationError):
        async with transaction(fixture.db.first):
            await fixture.db.first.execute(statement)
    assert await latest(fixture) == fixture.event.provider_timestamp


@pytest.mark.parametrize("whatsapp_relay_db", ["missing", "future"], indirect=True)
async def test_real_relay_persists_unavailable_without_post_or_retry(whatsapp_relay_db, monkeypatch):
    run = whatsapp_relay_db
    requests = []
    transport(monkeypatch, lambda request: requests.append(request))
    await relay.relay_batch(run.clock)
    async with transaction(run.fixture.db.second):
        row = await deliveries.get(run.intent.id)
    assert row is not None
    assert row.terminal_reason == "whatsapp_window_unavailable"
    assert row.delivered_at is None
    assert row.next_chunk == 0
    assert row.claim_token is row.lease_expires_at is None
    assert requests == []
    assert await relay.relay_batch(run.clock) == 0


async def test_real_existing_plan_expires_between_chunks_and_never_revives(whatsapp_relay_db, monkeypatch):
    run = whatsapp_relay_db
    sent = []

    def respond(request):
        sent.append(request)
        run.clock.now += timedelta(seconds=1)
        return httpx.Response(200, json={"messages": [{"id": "accepted"}]})

    transport(monkeypatch, respond)
    await relay.relay_batch(run.clock)
    async with transaction(run.fixture.db.second):
        partial = await deliveries.get(run.intent.id)
        await run.fixture.db.second.execute("UPDATE gateway_messages SET result = 'changed after restart'")
    await relay.relay_batch(run.clock)
    async with transaction(run.fixture.db.second):
        terminal = await deliveries.get(run.intent.id)
        await accept_channel_event(
            replace(run.fixture.event, event_key="new-inbound", provider_timestamp=datetime.now(UTC))
        )
    assert await relay.relay_batch(run.clock) == 0
    assert partial is not None
    assert terminal is not None
    assert partial.next_chunk == terminal.next_chunk == 1
    assert partial.im_plan == terminal.im_plan
    assert terminal.terminal_reason == "whatsapp_window_expired"
    assert terminal.delivered_at is None
    assert terminal.terminal_at == run.clock.now
    assert len(sent) == 1


async def test_real_restart_after_lost_checkpoint_uses_fresh_evidence(whatsapp_relay_db, monkeypatch):
    run = whatsapp_relay_db
    sent = []

    def respond(request):
        sent.append(request)
        return httpx.Response(200, json={"messages": [{"id": "accepted"}]})

    transport(monkeypatch, respond)
    checkpoint = deliveries.checkpoint_chunk
    monkeypatch.setattr(deliveries, "checkpoint_chunk", AsyncMock(return_value=False))
    await relay.relay_batch(run.clock)
    monkeypatch.setattr(deliveries, "checkpoint_chunk", checkpoint)
    run.clock.now += timedelta(seconds=61)
    await relay.relay_batch(run.clock)
    async with transaction(run.fixture.db.second):
        row = await deliveries.get(run.intent.id)
    assert row is not None
    assert row.attempts == 2
    assert row.next_chunk == 0
    assert row.im_plan is not None
    assert row.delivered_at is None
    assert row.terminal_reason == "whatsapp_window_expired"
    assert len(sent) == 1
