import inspect
import json
from contextlib import asynccontextmanager
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
from app.db import session as db_session
from app.db.errors import CheckViolationError
from app.db.session import get_connection, transaction
from app.services import agent_tools, gateway_report_relay as relay
from app.services.agent_tool_exec import channel_provider_whatsapp
from app.services.agent_tool_exec.registry import ToolArguments
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


def _inbound_timestamp(case: str) -> datetime | None:
    now = datetime.now(UTC)
    return {
        "fresh": now - timedelta(hours=1),
        "missing": None,
        "future": now + timedelta(hours=1),
        "expired": now - timedelta(hours=24, seconds=1),
    }[case]


@pytest.fixture
async def whatsapp_tool_db(channel_db, monkeypatch, request):
    """A bound contact with a real inbound receipt and reply session, reached through one shared connection."""
    fixture = channel_db

    class Pool:
        @asynccontextmanager
        async def connection(self):
            yield fixture.db.first.raw

    monkeypatch.setattr(db_session, "get_pool", lambda: Pool())
    timestamp = _inbound_timestamp(getattr(request, "param", "fresh"))
    async with transaction(fixture.db.first):
        await accept_channel_event(replace(fixture.event, provider_timestamp=timestamp))
        member = await fixture.db.first.fetchval("SELECT id FROM org_members")
        await fixture.db.first.execute(
            "INSERT INTO agent_relationships (id, agent_id, member_id, relation, description) "
            "VALUES (%(id)s, %(agent)s, %(member)s, 'collaborator', '')",
            {"id": uuid4(), "agent": fixture.event.agent_id, "member": member},
        )
    return fixture


def serve(monkeypatch, outcome):
    requests = []

    def respond(request):
        assert get_connection() is None, "the provider must never be called inside a database scope"
        requests.append(request)
        return outcome(request)

    transport(monkeypatch, respond)
    return requests


def accepted(_request):
    return httpx.Response(200, json={"messages": [{"id": "wamid.REAL"}]})


def timed_out(request):
    raise httpx.ReadTimeout("slow", request=request)


async def send_registered(fixture):
    handler = agent_tools.resolve_tool_handler("send_channel_message")
    assert handler is not None
    arguments: ToolArguments = {"member_name": "Actual sender", "message": "Following up", "channel": "whatsapp"}
    pending = handler(
        arguments=arguments, agent_id=fixture.event.agent_id, user_id=fixture.sender_id, session_id="", on_output=None
    )
    assert inspect.isawaitable(pending)
    return await pending


async def assistant_messages(fixture):
    async with transaction(fixture.db.first):
        return await fixture.db.first.fetchall(
            "SELECT m.content, m.user_id, s.external_conv_id FROM chat_messages m "
            "JOIN chat_sessions s ON s.id::text = m.conversation_id WHERE m.role = 'assistant'"
        )


async def test_registered_tool_posts_to_the_bound_session_and_commits_one_assistant_message(
    whatsapp_tool_db, monkeypatch
):
    requests = serve(monkeypatch, accepted)

    result = await send_registered(whatsapp_tool_db)

    assert result.startswith("✅")
    assert "wamid.REAL" in result
    assert [json.loads(request.content)["to"] for request in requests] == ["15550001"]
    rows = await assistant_messages(whatsapp_tool_db)
    assert [(row["content"], row["user_id"], row["external_conv_id"]) for row in rows] == [
        ("Following up", whatsapp_tool_db.sender_id, "whatsapp_15550001")
    ]


@pytest.mark.parametrize("whatsapp_tool_db", ["missing", "future", "expired"], indirect=True)
async def test_registered_tool_rejects_missing_future_or_old_receipt_before_http_or_history(
    whatsapp_tool_db, monkeypatch
):
    requests = serve(monkeypatch, accepted)

    result = await send_registered(whatsapp_tool_db)

    assert result.startswith("❌")
    assert requests == []
    assert await assistant_messages(whatsapp_tool_db) == []


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE channel_configs SET app_id = 'other-account'",
        "UPDATE channel_configs SET is_configured = FALSE",
        "UPDATE users SET is_active = FALSE WHERE display_name = 'Actual sender'",
        "UPDATE chat_sessions SET is_group = TRUE",
        "UPDATE chat_sessions SET user_id = (SELECT creator_id FROM agents)",
    ],
)
async def test_registered_tool_fails_closed_when_account_session_or_recipient_changes(
    whatsapp_tool_db, monkeypatch, statement
):
    async with transaction(whatsapp_tool_db.db.first):
        await whatsapp_tool_db.db.first.execute(statement)
    requests = serve(monkeypatch, accepted)

    result = await send_registered(whatsapp_tool_db)

    assert result.startswith("❌")
    assert requests == []
    assert await assistant_messages(whatsapp_tool_db) == []


async def test_registered_tool_timeout_is_uncertain_unrecorded_and_not_retried(whatsapp_tool_db, monkeypatch):
    requests = serve(monkeypatch, timed_out)

    result = await send_registered(whatsapp_tool_db)

    assert result.startswith("⚠️")
    assert len(requests) == 1
    assert await assistant_messages(whatsapp_tool_db) == []


async def test_registered_tool_history_failure_after_acknowledgement_leaves_no_partial_history(
    whatsapp_tool_db, monkeypatch
):
    requests = serve(monkeypatch, accepted)

    async def fail(**_fields):
        raise RuntimeError("history update failed")

    monkeypatch.setattr(channel_provider_whatsapp.chat_session_dao, "update", fail)

    result = await send_registered(whatsapp_tool_db)

    assert result.startswith("⚠️")
    assert "wamid.REAL" in result
    assert len(requests) == 1
    assert await assistant_messages(whatsapp_tool_db) == []
