from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from tests.test_chat_submission_postgres import ReceiptDatabase, receipt_db

__all__ = ["receipt_db"]

from app.api import background_tasks
from app.core import access_cache
from app.dao import channel_config_dao
from app.dao.channel_event_receipt_dao import channel_event_receipt_dao
from app.dao.gateway_report_delivery_dao import gateway_report_delivery_dao
from app.db import session as db_session
from app.db.session import transaction
from app.records.agent import AgentRecord
from app.records.channel_config import ChannelConfigRecord
from app.records.channel_event_receipt import ChannelReplyScope
from app.records.gateway_report_delivery import GatewayReportDeliveryRecord
from app.services import openclaw_routing
from app.services.channels import submissions
from app.services.channels.submissions import ChannelEvent


@dataclass(frozen=True, slots=True)
class ChannelDatabase:
    db: ReceiptDatabase
    config: ChannelConfigRecord
    event: ChannelEvent
    sender_id: UUID
    tenant_id: UUID


@pytest.fixture
async def channel_db(receipt_db: ReceiptDatabase, monkeypatch: pytest.MonkeyPatch):
    db = receipt_db
    tenant_id, provider_id, sender_id, config_id = (uuid4() for _ in range(4))
    async with transaction(db.first):
        await db.first.execute(
            "INSERT INTO tenants (id, name, slug) VALUES (%(id)s, 'Channel QA', 'channel-qa')", {"id": tenant_id}
        )
        await db.first.execute(
            "INSERT INTO identity_providers (id, provider_type, name, tenant_id) "
            "VALUES (%(id)s, 'whatsapp', 'Channel QA', %(tenant)s)",
            {"id": provider_id, "tenant": tenant_id},
        )
        await db.first.execute("UPDATE users SET tenant_id = %(tenant)s", {"tenant": tenant_id})
        await db.first.execute(
            "INSERT INTO users (id, display_name, tenant_id, quota_message_limit) "
            "VALUES (%(id)s, 'Actual sender', %(tenant)s, 2)",
            {"id": sender_id, "tenant": tenant_id},
        )
        await db.first.execute(
            "UPDATE agents SET tenant_id = %(tenant)s, access_mode = 'company' WHERE id = %(id)s",
            {"tenant": tenant_id, "id": db.agent.id},
        )
        await db.first.execute(
            "INSERT INTO org_members (id, external_id, provider_id, name, title, status, tenant_id, user_id) "
            "VALUES (%(id)s, '15550001', %(provider)s, 'Actual sender', '', 'active', %(tenant)s, %(user)s)",
            {"id": uuid4(), "provider": provider_id, "tenant": tenant_id, "user": sender_id},
        )
        await db.first.execute(
            "INSERT INTO channel_configs (id, agent_id, channel_type, app_id, app_secret, encrypt_key, "
            "is_configured, is_connected) VALUES (%(id)s, %(agent)s, 'whatsapp', 'phone-account', "
            "'access-token', 'app-secret', TRUE, FALSE)",
            {"id": config_id, "agent": db.agent.id},
        )
        config = await channel_config_dao.get(config_id)
        assert config is not None

    async def prepare(*, agent: AgentRecord, content: str, history):
        return openclaw_routing.PreparedOpenClawMessage(agent, content, "primary", None, None, "channel-qa")

    async def acl_version(_agent_id: UUID):
        return 0

    monkeypatch.setattr(submissions, "prepare_openclaw_message", prepare)
    monkeypatch.setattr(access_cache, "read_acl_version", acl_version)
    monkeypatch.setattr(background_tasks, "schedule_background_task", lambda coroutine, _description: coroutine.close())
    event = ChannelEvent(
        config_id=config_id,
        agent_id=db.agent.id,
        provider="whatsapp",
        event_key="native-1",
        sender_id="15550001",
        conversation_id="whatsapp_15550001",
        text="identical text",
        provider_timestamp=datetime.now(UTC) - timedelta(hours=1),
        provider_account_id="phone-account",
    )
    return ChannelDatabase(db, config, event, sender_id, tenant_id)


@dataclass(slots=True)
class Clock:
    now: datetime

    def __call__(self) -> datetime:
        return self.now


def scope_for(fixture: ChannelDatabase) -> ChannelReplyScope:
    return ChannelReplyScope(
        fixture.config.id,
        fixture.event.agent_id,
        fixture.tenant_id,
        "whatsapp",
        "phone-account",
        fixture.event.conversation_id,
    )


@dataclass(frozen=True, slots=True)
class WhatsAppRelayDatabase:
    fixture: ChannelDatabase
    intent: GatewayReportDeliveryRecord
    clock: Clock


@pytest.fixture
async def whatsapp_relay_db(channel_db, monkeypatch, request):
    fixture = channel_db
    clock = Clock(datetime.now(UTC) + timedelta(seconds=2))
    timestamp = clock.now - timedelta(hours=24) + timedelta(seconds=1)
    if getattr(request, "param", None) == "missing":
        timestamp = None
    if getattr(request, "param", None) == "future":
        timestamp = clock.now + timedelta(hours=1)

    class Pool:
        @asynccontextmanager
        async def connection(self):
            yield fixture.db.first.raw

    monkeypatch.setattr(db_session, "get_pool", lambda: Pool())
    async with transaction(fixture.db.first):
        await submissions.accept_channel_event(replace(fixture.event, provider_timestamp=timestamp))
        await fixture.db.first.execute(
            "UPDATE gateway_messages SET status = 'completed', result = %(text)s", {"text": "a" * 4096 + "second chunk"}
        )
        row = await fixture.db.first.fetchone("SELECT id FROM gateway_report_deliveries")
        assert row is not None
        intent = await gateway_report_delivery_dao.get(row["id"])
        assert intent is not None
    return WhatsAppRelayDatabase(fixture, intent, clock)


async def latest(fixture: ChannelDatabase):
    async with transaction(fixture.db.first):
        return await channel_event_receipt_dao.latest_eligible_timestamp(scope_for(fixture))
