from __future__ import annotations

import importlib
import json
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException
from tests.test_channel_delivery_transport import transport

from app.core.domain.channels import ChannelType
from app.dao.channel_event_receipt_dao import channel_event_receipt_dao
from app.records.agent import AgentRecord
from app.records.agent_relationship import AgentRelationshipRecord
from app.records.channel_config import ChannelConfigRecord
from app.records.channel_event_receipt import ChannelReplyScope
from app.records.chat import ChatSessionRecord
from app.records.identity import IdentityProviderRecord
from app.records.org import OrgMemberRecord
from app.records.user import UserRecord
from app.services import activity_logger, agent_tools, okr_daily_collection
from app.services.agent_tool_exec import (
    channel_context,
    channel_messaging,
    channel_provider_whatsapp as provider,
    channel_providers,
    dispatcher,
)
from app.services.agent_tool_exec.registry import ToolArguments
from app.services.channel_session import find_or_create_channel_session
from app.services.channel_user_service import get_platform_user_by_org_member
from app.services.channels import outbound_plan


def test_channel_contextvars_are_exported_from_channel_context() -> None:
    context = importlib.import_module("app.services.agent_tool_exec.channel_context")

    assert channel_context.channel_file_sender is context.channel_file_sender
    assert channel_context.channel_web_agent_id is context.channel_web_agent_id
    assert channel_context.channel_feishu_sender_open_id is context.channel_feishu_sender_open_id


def test_channel_runtime_dependencies_import_from_owners() -> None:
    assert find_or_create_channel_session is not None
    assert get_platform_user_by_org_member is not None


async def test_send_channel_file_uses_sender_contextvar(tmp_path: Path) -> None:
    channel_files = importlib.import_module("app.services.agent_tool_exec.channel_files")
    agent_id = uuid.uuid4()
    source_file = tmp_path / "report.txt"
    source_file.write_text("daily report", encoding="utf-8")
    calls: list[tuple[Path, str]] = []

    async def sender(file_path: Path, message: str = "") -> None:
        calls.append((file_path, message))

    token = channel_context.channel_file_sender.set(sender)
    try:
        result = await channel_files._send_channel_file(
            agent_id,
            tmp_path,
            {"file_path": "report.txt", "message": "please review"},
        )
    finally:
        channel_context.channel_file_sender.reset(token)

    assert result == "File 'report.txt' sent to user via channel."
    assert calls == [(source_file.resolve(), "please review")]


async def test_send_channel_file_missing_sender_keeps_download_and_error_behavior(tmp_path: Path) -> None:
    channel_files = importlib.import_module("app.services.agent_tool_exec.channel_files")
    agent_id = uuid.uuid4()
    source_file = tmp_path / "report.txt"
    source_file.write_text("daily report", encoding="utf-8")

    token = channel_context.channel_file_sender.set(None)
    try:
        result = await channel_files._send_channel_file(
            agent_id,
            tmp_path,
            {"file_path": "report.txt"},
        )
    finally:
        channel_context.channel_file_sender.reset(token)

    assert "no active channel sender" in result.lower() or "download" in result.lower() or "❌" in result


def test_okr_daily_collection_imports_channel_message_from_owner() -> None:
    from app.services import okr_daily_collection
    from app.services.agent_tool_exec.channel_messaging import _send_channel_message

    assert okr_daily_collection._send_channel_message is _send_channel_message


async def _value(value):
    return value


@pytest.fixture
def whatsapp(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """An eligible tenant, bound contact and live reply session, with only the provider HTTP edge faked."""
    tenant_id, user_id = uuid.uuid4(), uuid.uuid4()
    agent = AgentRecord(id=uuid.uuid4(), name="Assistant", creator_id=uuid.uuid4(), tenant_id=tenant_id)
    row = IdentityProviderRecord(id=uuid.uuid4(), provider_type="whatsapp", name="WhatsApp", tenant_id=tenant_id)
    member = OrgMemberRecord(
        id=uuid.uuid4(),
        name="Alex",
        external_id="15550001",
        provider_id=row.id,
        tenant_id=tenant_id,
        user_id=user_id,
    )
    world = SimpleNamespace(
        agent=agent,
        member=member,
        provider=row,
        relationships=[
            AgentRelationshipRecord(uuid.uuid4(), agent.id, member.id, member=member, provider_type="whatsapp")
        ],
        principal=UserRecord(id=user_id, tenant_id=tenant_id),
        session=ChatSessionRecord(
            id=uuid.uuid4(),
            agent_id=agent.id,
            user_id=user_id,
            source_channel="whatsapp",
            external_conv_id="whatsapp_15550001",
        ),
        config=ChannelConfigRecord(
            uuid.uuid4(), agent.id, ChannelType.WHATSAPP, app_id="phone-account", app_secret="token", is_configured=True
        ),
        access="use",
        tenant_active=True,
        receipt=datetime.now(UTC) - timedelta(hours=1),
        history_error=None,
        script=lambda _request: httpx.Response(200, json={"messages": [{"id": "wamid.ACK"}]}),
        requests=[],
        events=[],
        saved=[],
        scopes=[],
        lookups=[],
    )

    async def get_principal(_user_id, *, fresh=False):
        if world.principal is None:
            raise HTTPException(401)
        return world.principal

    async def latest(scope):
        world.events.append("window")
        world.scopes.append(scope)
        return world.receipt

    async def get_session(*, agent_id, external_conv_id):
        world.lookups.append(external_conv_id)
        return world.session

    async def insert(**fields):
        if world.history_error is not None:
            raise world.history_error
        world.events.append("insert")
        world.saved.append(fields)

    @asynccontextmanager
    async def connection():
        world.events.append("tx-open")
        yield
        world.events.append("tx-close")

    def respond(request):
        world.events.append("http")
        world.requests.append(request)
        return world.script(request)

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("no identity or fallback conversation may be created or searched")

    provider_dao = provider.chat_session_dao
    for name in ("create", "list_for_user", "list_for_agent_channel"):
        monkeypatch.setattr(provider_dao, name, forbidden)
    monkeypatch.setattr(provider_dao, "get_by_external_conv", get_session)
    monkeypatch.setattr(provider_dao, "update", lambda **_fields: _value(None))
    monkeypatch.setattr(provider.chat_message_dao, "insert_message", insert)
    monkeypatch.setattr(provider, "connection_ctx", connection)
    monkeypatch.setattr(provider, "load_user_principal", get_principal)
    monkeypatch.setattr(provider, "compute_agent_access_level", lambda *_args: _value(world.access))
    monkeypatch.setattr(provider.agent_dao, "get", lambda _id: _value(world.agent))
    monkeypatch.setattr(provider.identity_provider_dao, "get_by_type_and_tenant", lambda *_args: _value(world.provider))
    monkeypatch.setattr(
        provider.company_admission_dao, "tenant", lambda _id: _value(SimpleNamespace(is_active=world.tenant_active))
    )
    monkeypatch.setattr(
        outbound_plan.channel_config_dao, "get_configured_for_agent", lambda *_args, **_kw: _value(world.config)
    )
    monkeypatch.setattr(channel_event_receipt_dao, "latest_eligible_timestamp", latest)
    monkeypatch.setattr(
        channel_messaging.agent_relationship_dao,
        "list_for_agent_with_members_and_providers",
        lambda _agent_id: _value(world.relationships),
    )
    monkeypatch.setattr(
        channel_messaging, "evaluate_human_relationship_status", lambda *_a, **_k: _value({"access_status": "active"})
    )
    monkeypatch.setattr(agent_tools, "_get_agent_tenant_id", lambda _id: _value(str(tenant_id)))
    monkeypatch.setattr(agent_tools, "_agent_workspace_root", lambda _id: Path())
    monkeypatch.setattr(activity_logger, "log_activity", lambda *_a, **_k: _value(None))
    transport(monkeypatch, respond)
    return world


async def send(world: SimpleNamespace, **overrides: str) -> str:
    """Run the registered tool through the public dispatcher."""
    arguments: ToolArguments = {"member_name": "Alex", "message": "Hello Alex", "channel": "whatsapp", **overrides}
    return await dispatcher.execute_tool("send_channel_message", arguments, world.agent.id, uuid.uuid4())


async def test_registered_whatsapp_send_checks_receipt_then_posts_then_records_history(whatsapp):
    result = await send(whatsapp)

    assert result.startswith("✅")
    assert "wamid.ACK" in result
    assert "delivered" not in result.lower()
    [request] = whatsapp.requests
    assert str(request.url) == "https://graph.facebook.com/v23.0/phone-account/messages"
    assert request.headers["Authorization"] == "Bearer token"
    assert json.loads(request.content) == {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": "15550001",
        "type": "text",
        "text": {"preview_url": False, "body": "Hello Alex"},
    }
    assert whatsapp.lookups == ["whatsapp_15550001"]
    assert whatsapp.scopes == [
        ChannelReplyScope(
            whatsapp.config.id,
            whatsapp.agent.id,
            whatsapp.agent.tenant_id,
            "whatsapp",
            "phone-account",
            "whatsapp_15550001",
        )
    ]
    # The database transaction opens only after the provider call has returned.
    assert whatsapp.events == ["window", "http", "tx-open", "insert", "tx-close"]
    assert whatsapp.saved == [
        {
            "agent_id": whatsapp.agent.id,
            "user_id": whatsapp.session.user_id,
            "role": "assistant",
            "content": "Hello Alex",
            "conversation_id": str(whatsapp.session.id),
        }
    ]


@pytest.mark.parametrize(
    ("text", "attempted"), [("x" * 4096, True), ("x" * 4097, False), ("   ", False), ("\n", False)]
)
async def test_registered_whatsapp_send_bounds_text_before_any_io(whatsapp, text, attempted):
    result = await send(whatsapp, message=text)

    assert result.startswith("✅" if attempted else "❌")
    assert len(whatsapp.requests) == int(attempted)
    assert whatsapp.events == (["window", "http", "tx-open", "insert", "tx-close"] if attempted else [])


@pytest.mark.parametrize(
    "mutate",
    [
        lambda w: setattr(w, "session", None),
        lambda w: setattr(w.session, "is_group", True),
        lambda w: setattr(w.session, "user_id", uuid.uuid4()),
        lambda w: setattr(w.session, "source_channel", "slack"),
        lambda w: setattr(w.member, "tenant_id", uuid.uuid4()),
        lambda w: setattr(w.member, "user_id", None),
        lambda w: setattr(w.session, "external_conv_id", "whatsapp_15559999"),
        lambda w: setattr(w.member, "external_id", ""),
        lambda w: setattr(w, "provider", None),
        lambda w: setattr(w.provider, "is_active", False),
        lambda w: setattr(w.provider, "id", uuid.uuid4()),
        lambda w: setattr(w, "principal", None),
        lambda w: setattr(w.principal, "tenant_id", uuid.uuid4()),
        lambda w: setattr(w, "tenant_active", False),
        lambda w: setattr(w, "access", None),
        lambda w: setattr(w.agent, "tenant_id", None),
        lambda w: setattr(w, "config", None),
        lambda w: setattr(w.config, "extra_config", {"mode": "personal_qr"}),
        lambda w: setattr(w.config, "app_secret", ""),
    ],
    ids=[
        "no-session",
        "group-session",
        "other-users-session",
        "other-channel-session",
        "other-tenant-member",
        "unbound-member",
        "session-of-another-phone",
        "no-phone",
        "no-provider",
        "inactive-provider",
        "other-provider",
        "inactive-recipient",
        "recipient-other-tenant",
        "inactive-company",
        "no-agent-access",
        "agent-without-tenant",
        "no-config",
        "personal-qr-config",
        "no-credentials",
    ],
)
async def test_registered_whatsapp_send_fails_closed_before_receipt_or_http(whatsapp, mutate):
    mutate(whatsapp)

    result = await send(whatsapp)

    assert result.startswith("❌")
    assert whatsapp.events == []
    assert whatsapp.requests == []
    assert whatsapp.saved == []


async def test_registered_whatsapp_send_never_enqueues_on_a_personal_qr_route(whatsapp, monkeypatch):
    from app.dao import whatsapp_personal_dao
    from app.records.whatsapp_personal import PersonalRoute
    from app.services.channels import whatsapp_personal_journal

    route = PersonalRoute(
        config_id=whatsapp.config.id,
        auth_epoch=uuid.uuid4(),
        generation=uuid.uuid4(),
        account_id="account",
        self_jid="self",
    ).external_id()
    whatsapp.member.external_id = route.removeprefix("whatsapp_")
    whatsapp.session.external_conv_id = route
    whatsapp.config.extra_config = {"mode": "personal_qr"}
    enqueued = []

    async def enqueue(*args):
        enqueued.append(args)

    monkeypatch.setattr(whatsapp_personal_dao, "get_personal", lambda _agent_id: _value(SimpleNamespace()))
    monkeypatch.setattr(whatsapp_personal_journal, "route_matches", lambda *_args: True)
    monkeypatch.setattr(whatsapp_personal_journal, "enqueue", enqueue)

    result = await send(whatsapp)

    assert result.startswith("❌")
    assert enqueued == []
    assert whatsapp.events == []
    assert whatsapp.saved == []


@pytest.mark.parametrize(
    "age", [None, timedelta(hours=-1), timedelta(hours=24, seconds=1)], ids=["missing", "future", "expired"]
)
async def test_registered_whatsapp_send_rejects_missing_future_or_old_receipt_before_http(whatsapp, age):
    whatsapp.receipt = None if age is None else datetime.now(UTC) - age

    result = await send(whatsapp)

    assert result.startswith("❌")
    assert whatsapp.events == ["window"]
    assert whatsapp.requests == []
    assert whatsapp.saved == []


def _raise_timeout(request):
    raise httpx.ReadTimeout("slow", request=request)


@pytest.mark.parametrize(
    ("script", "prefix"),
    [
        (lambda _request: httpx.Response(200, json={}), "⚠️"),
        (lambda _request: httpx.Response(200, json={"messages": [{}]}), "⚠️"),
        (lambda _request: httpx.Response(200, json={"messages": [{"id": 12345}]}), "⚠️"),
        (_raise_timeout, "⚠️"),
        (lambda _request: httpx.Response(503, json={}), "⚠️"),
        (lambda _request: httpx.Response(400, json={"error": {"code": 131047}}), "❌"),
    ],
    ids=["no-ack", "no-message-id", "non-string-id", "timeout", "server-error", "rejected"],
)
async def test_registered_whatsapp_send_without_acknowledgement_is_uncertain_unrecorded_and_not_retried(
    whatsapp, script, prefix
):
    whatsapp.script = script

    result = await send(whatsapp)

    assert result.startswith(prefix)
    assert "delivered" not in result.lower()
    assert len(whatsapp.requests) == 1
    assert whatsapp.saved == []
    assert "tx-open" not in whatsapp.events


async def test_registered_whatsapp_acknowledgement_then_history_failure_is_accepted_but_unrecorded(whatsapp):
    whatsapp.history_error = RuntimeError("database unavailable")

    result = await send(whatsapp)

    assert result.startswith("⚠️")
    assert "wamid.ACK" in result
    assert "delivered" not in result.lower()
    assert len(whatsapp.requests) == 1
    assert whatsapp.saved == []


async def test_channel_selection_and_other_senders_are_unchanged_by_whatsapp(whatsapp, monkeypatch):
    member = OrgMemberRecord(
        id=uuid.uuid4(), name="Alex", external_id="ou_alex", tenant_id=whatsapp.agent.tenant_id, user_id=uuid.uuid4()
    )
    feishu = AgentRelationshipRecord(uuid.uuid4(), whatsapp.agent.id, member.id, member=member, provider_type="feishu")
    sent = []

    async def send_feishu(agent_id, member_name, message_text, target_member):
        sent.append((member_name, message_text, target_member.id))
        return "✅ Feishu sender ran"

    monkeypatch.setattr(channel_providers, "_send_feishu_channel_message", send_feishu)
    whatsapp.relationships = [feishu, *whatsapp.relationships]
    omitted: ToolArguments = {"member_name": "Alex", "message": "Daily report?"}

    # OKR outreach omits the channel: the first relationship still wins, so WhatsApp is not tried.
    assert await okr_daily_collection._send_channel_message(whatsapp.agent.id, omitted) == "✅ Feishu sender ran"
    assert await send(whatsapp, channel="feishu") == "✅ Feishu sender ran"
    assert whatsapp.requests == []
    assert sent == [("Alex", "Daily report?", member.id), ("Alex", "Hello Alex", member.id)]
    assert (await send(whatsapp)).startswith("✅")
    assert len(whatsapp.requests) == 1

    # A WhatsApp-only relationship reached with the channel omitted uses the same guarded sender.
    whatsapp.relationships = whatsapp.relationships[1:]
    assert (await okr_daily_collection._send_channel_message(whatsapp.agent.id, omitted)).startswith("✅")
    assert len(whatsapp.requests) == 2


def test_send_channel_message_schema_offers_whatsapp_through_the_one_existing_tool():
    from app.services.agent_tools_definitions import AGENT_TOOLS

    tools = [json.loads(json.dumps(tool))["function"] for tool in AGENT_TOOLS if "whatsapp" in json.dumps(tool).lower()]

    assert [tool["name"] for tool in tools] == ["send_channel_message"]
    assert "whatsapp" in tools[0]["parameters"]["properties"]["channel"]["enum"]
