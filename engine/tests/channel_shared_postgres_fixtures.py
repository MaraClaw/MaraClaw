from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI

from app.api import background_tasks, slack
from app.dao import agent_dao
from app.db.connection import DbConnection
from app.records.agent import AgentRecord
from app.scripts import bootstrap_db
from app.services import openclaw_routing
from app.services.channels import binding, llm_bridge
from test_bootstrap_postgres import schema_db as schema_db


@dataclass(frozen=True, slots=True)
class LiveChannel:
    db: DbConnection
    agent: AgentRecord
    identities: tuple[UUID, ...]
    users: tuple[UUID, ...]
    client: httpx.AsyncClient
    published: AsyncMock


@pytest.fixture
async def live_channel(schema_db, monkeypatch):
    await bootstrap_db.main()
    await bootstrap_db.init_pool()
    tenant_id, agent_id, provider_id = uuid4(), uuid4(), uuid4()
    identities = tuple(uuid4() for _ in range(3))
    users = tuple(uuid4() for _ in range(3))
    await schema_db.execute(
        "INSERT INTO tenants (id, name, slug) VALUES (%(id)s, 'Channel QA', 'channel-qa')", {"id": tenant_id}
    )
    for index, (identity_id, user_id) in enumerate(zip(identities, users, strict=True)):
        await schema_db.execute("INSERT INTO identities (id) VALUES (%(id)s)", {"id": identity_id})
        await schema_db.execute(
            "INSERT INTO users (id, identity_id, tenant_id, display_name) "
            "VALUES (%(id)s, %(identity)s, %(tenant)s, %(name)s)",
            {"id": user_id, "identity": identity_id, "tenant": tenant_id, "name": f"Human {index}"},
        )
    await schema_db.execute(
        "INSERT INTO agents (id, name, role_description, creator_id, tenant_id, max_triggers, "
        "min_poll_interval_min, webhook_rate_limit, access_mode, company_access_level, heartbeat_interval_minutes) "
        "VALUES (%(id)s, 'Channel QA', 'QA', %(creator)s, %(tenant)s, 20, 5, 5, 'company', 'use', 240)",
        {"id": agent_id, "creator": users[2], "tenant": tenant_id},
    )
    await schema_db.execute(
        "INSERT INTO identity_providers (id, provider_type, name, tenant_id) "
        "VALUES (%(id)s, 'slack', 'Slack', %(tenant)s)",
        {"id": provider_id, "tenant": tenant_id},
    )
    for index, user_id in enumerate(users[:2]):
        await schema_db.execute(
            "INSERT INTO org_members (id, provider_id, user_id, tenant_id, external_id, name, title, status) "
            "VALUES (%(id)s, %(provider)s, %(user)s, %(tenant)s, %(external)s, 'Human', '', 'active')",
            {"id": uuid4(), "provider": provider_id, "user": user_id, "tenant": tenant_id, "external": f"U{index}"},
        )
    await schema_db.execute(
        "INSERT INTO channel_configs (id, agent_id, channel_type, encrypt_key, is_configured, is_connected) "
        "VALUES (%(id)s, %(agent)s, 'slack', 'qa-secret', true, true)",
        {"id": uuid4(), "agent": agent_id},
    )
    agent = await agent_dao.get(agent_id)
    assert agent is not None

    async def prepare(*, agent, content, history=None):
        return openclaw_routing.PreparedOpenClawMessage(agent, content, "primary", None, None, "qa")

    monkeypatch.setattr(openclaw_routing, "prepare_openclaw_message", prepare)
    monkeypatch.setattr(openclaw_routing, "_schedule_inbox_wake", lambda *args: None)
    monkeypatch.setattr(llm_bridge, "_load_agent_and_model", AsyncMock(return_value=(agent, None, None)))
    published = AsyncMock()
    monkeypatch.setattr(binding, "_publish_shared_user_message", published)
    app = FastAPI()
    app.include_router(slack.router, prefix="/api")
    background_tasks.start_background_tasks()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url="http://qa"
    ) as client:
        try:
            yield LiveChannel(schema_db, agent, identities, users, client, published)
        finally:
            await background_tasks.drain_background_tasks()


async def post_slack(live: LiveChannel, *, sender="U0", text="hello", channel="D0", valid_signature=True):
    body = json.dumps(
        {"type": "event_callback", "event": {"type": "message", "user": sender, "channel": channel, "text": text}}
    ).encode()
    stamp = str(int(time.time()))
    signature = hmac.new(b"qa-secret", b"v0:" + stamp.encode() + b":" + body, hashlib.sha256).hexdigest()
    return await live.client.post(
        f"/api/channel/slack/{live.agent.id}/webhook",
        content=body,
        headers={
            "x-slack-request-timestamp": stamp,
            "x-slack-signature": f"v0={signature}" if valid_signature else "bad",
        },
    )
