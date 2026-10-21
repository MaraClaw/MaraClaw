from __future__ import annotations

import uuid
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.records.agent import AgentRecord
from app.records.chat import ChatSessionRecord
from app.records.identity import IdentityProviderRecord
from app.records.org import OrgMemberRecord
from app.records.tenant import TenantRecord
from app.records.user import UserRecord
from app.services.channel_user_service import ChannelUserResolutionError
from app.services.channels import binding, inbound
from app.services.shared_chat import shared_session_id


@pytest.fixture
def context(monkeypatch):
    tenant_id = uuid.uuid4()
    user = UserRecord(id=uuid.uuid4(), tenant_id=tenant_id)
    agent = AgentRecord(id=uuid.uuid4(), creator_id=uuid.uuid4(), tenant_id=tenant_id, name="Bot")
    provider = IdentityProviderRecord(id=uuid.uuid4(), provider_type="slack", name="Slack", tenant_id=tenant_id)
    member = OrgMemberRecord(id=uuid.uuid4(), provider_id=provider.id, user_id=user.id, name="Human")
    shared = ChatSessionRecord(
        id=shared_session_id(agent.id), agent_id=agent.id, user_id=agent.creator_id, source_channel="shared"
    )
    monkeypatch.setattr(binding.identity_provider_dao, "get_by_type_and_tenant", AsyncMock(return_value=provider))
    monkeypatch.setattr(binding.channel_user_service, "_find_org_member", AsyncMock(return_value=member))
    monkeypatch.setattr(binding.user_dao, "get_with_identity", AsyncMock(return_value=user))
    monkeypatch.setattr(binding, "load_user_principal", AsyncMock(return_value=user))
    monkeypatch.setattr(binding, "check_agent_access", AsyncMock(return_value=(agent, "use")))
    monkeypatch.setattr(
        binding.company_admission_dao, "tenant", AsyncMock(return_value=TenantRecord(tenant_id, "Company", "company"))
    )
    monkeypatch.setattr(binding, "ensure_shared_session", AsyncMock(return_value=shared))
    return agent, user, shared


@pytest.mark.parametrize(
    "source",
    ["slack", "feishu", "dingtalk", "google_chat", "microsoft_teams", "wecom", "whatsapp", "discord", "wechat"],
)
async def test_direct_binding_separates_transcript_from_provider_address(context, source):
    # Given
    agent, user, shared = context
    provider = ChatSessionRecord(
        id=uuid.uuid4(), agent_id=agent.id, user_id=user.id, source_channel=source, external_conv_id="provider-address"
    )
    # When
    conversation = await binding.bind_channel_conversation(agent=agent, platform_user=user, reply_session=provider)
    # Then
    assert conversation.transcript_session is shared
    assert conversation.reply_session is provider
    assert provider.source_channel == source
    assert provider.external_conv_id == "provider-address"
    assert isinstance(binding.load_user_principal, AsyncMock)
    assert isinstance(binding.check_agent_access, AsyncMock)
    binding.load_user_principal.assert_awaited_once_with(user.id, fresh=True)
    binding.check_agent_access.assert_awaited_once_with(user, agent.id, fresh=True)


async def test_group_binding_keeps_private_provider_history(context):
    # Given
    agent, user, _ = context
    group = ChatSessionRecord(
        id=uuid.uuid4(), agent_id=agent.id, user_id=user.id, source_channel="slack", is_group=True
    )
    # When
    conversation = await binding.bind_channel_conversation(agent=agent, platform_user=user, reply_session=group)
    # Then
    assert conversation.transcript_session is group
    assert isinstance(binding.ensure_shared_session, AsyncMock)
    binding.ensure_shared_session.assert_not_awaited()


async def test_group_event_keeps_legacy_provider_session_without_group_flag(context):
    # Given
    agent, user, _ = context
    route = ChatSessionRecord(id=uuid.uuid4(), agent_id=agent.id, user_id=user.id, source_channel="dingtalk")
    # When
    conversation = await binding.bind_channel_conversation(
        agent=agent, platform_user=user, reply_session=route, is_group=True
    )
    # Then
    assert conversation.transcript_session is route
    assert isinstance(binding.ensure_shared_session, AsyncMock)
    binding.ensure_shared_session.assert_not_awaited()


@pytest.mark.parametrize("rejection", ["principal", "tenant", "access"])
async def test_denied_direct_sender_cannot_open_shared_context(context, monkeypatch, rejection):
    # Given
    agent, user, _ = context
    route = ChatSessionRecord(id=uuid.uuid4(), agent_id=agent.id, user_id=user.id, source_channel="slack")
    assert isinstance(binding.load_user_principal, AsyncMock)
    assert isinstance(binding.check_agent_access, AsyncMock)
    assert isinstance(binding.ensure_shared_session, AsyncMock)
    if rejection == "principal":
        binding.load_user_principal.side_effect = HTTPException(403)
    elif rejection == "tenant":
        monkeypatch.setattr(binding.company_admission_dao, "tenant", AsyncMock(return_value=None))
    else:
        binding.check_agent_access.side_effect = HTTPException(403)
    # When / Then
    with pytest.raises((HTTPException, ChannelUserResolutionError)):
        await binding.bind_channel_conversation(agent=agent, platform_user=user, reply_session=route)
    binding.ensure_shared_session.assert_not_awaited()


async def test_unbound_external_sender_never_uses_creator_or_lazy_identity(context, monkeypatch):
    # Given
    agent, _, _ = context
    monkeypatch.setattr(binding.channel_user_service, "_find_org_member", AsyncMock(return_value=None))
    lazy_resolver = AsyncMock()
    monkeypatch.setattr(binding.channel_user_service, "resolve_channel_user", lazy_resolver)
    # When / Then
    with pytest.raises(ChannelUserResolutionError):
        await inbound.resolve_sender_user(agent=agent, channel_type="slack", external_user_id="unknown")
    lazy_resolver.assert_not_awaited()
    assert isinstance(binding.ensure_shared_session, AsyncMock)
    binding.ensure_shared_session.assert_not_awaited()


async def test_generate_reply_preserves_explicit_provider_route(context, monkeypatch):
    # Given
    agent, user, shared = context
    route_id = uuid.uuid4()
    enqueue = AsyncMock()
    monkeypatch.setattr("app.services.openclaw_routing.enqueue_openclaw_message", enqueue)
    # When
    await inbound.generate_channel_reply(
        agent_id=agent.id,
        user_text="hello",
        history=[],
        user_id=user.id,
        session_id=str(shared.id),
        reply_session_id=route_id,
        agent_model=agent,
    )
    # Then
    assert enqueue.await_args is not None
    assert enqueue.await_args.kwargs["conversation_id"] == str(shared.id)
    assert enqueue.await_args.kwargs["reply_session_id"] == route_id


async def test_enqueue_failure_rolls_back_channel_user_message(context, monkeypatch):
    # Given
    from contextlib import asynccontextmanager

    agent, user, shared = context
    writes = []

    @asynccontextmanager
    async def transaction():
        try:
            yield
        except RuntimeError:
            writes.clear()
            raise

    async def persist(**kwargs):
        writes.append(kwargs)

    monkeypatch.setattr(inbound, "optional_connection_ctx", transaction)
    monkeypatch.setattr(inbound, "persist_channel_user_message", persist)
    monkeypatch.setattr(
        "app.services.openclaw_routing.enqueue_openclaw_message", AsyncMock(side_effect=RuntimeError("queue failed"))
    )
    # When / Then
    with pytest.raises(RuntimeError, match="queue failed"):
        await inbound.generate_channel_reply(
            agent_id=agent.id,
            user_text="with runtime markers",
            history=[],
            user_id=user.id,
            session_id=str(shared.id),
            reply_session_id=uuid.uuid4(),
            agent_model=agent,
            user_message_content="visible attachment message",
        )
    assert writes == []


async def test_shared_user_event_is_deferred_until_message_commit(context, monkeypatch):
    # Given
    from app.db import session as db_session
    from app.records.chat import ChatMessageRecord
    from test_db_session import _FakePool, _FakeRawConnection

    agent, user, shared = context
    message = ChatMessageRecord(
        id=uuid.uuid4(),
        agent_id=agent.id,
        user_id=user.id,
        role="user",
        content="hello",
        conversation_id=str(shared.id),
    )
    raw = _FakeRawConnection()
    monkeypatch.setattr(db_session, "get_pool", lambda: _FakePool(raw))
    monkeypatch.setattr(binding.chat_message_dao, "insert_message", AsyncMock(return_value=message))
    monkeypatch.setattr(binding.chat_session_dao, "get", AsyncMock(return_value=shared))
    monkeypatch.setattr(binding.chat_session_dao, "update", AsyncMock())
    scheduled = []

    def schedule(coroutine, description):
        db_session.run_after_commit(lambda: scheduled.append((coroutine, description)), coroutine.close)

    monkeypatch.setattr("app.api.background_tasks.schedule_background_task", schedule)
    # When
    try:
        async with db_session.connection_ctx():
            await inbound.persist_user_message(agent_id=agent.id, user_id=user.id, session=shared, content="hello")
            assert scheduled == []
        # Then
        assert len(scheduled) == 1
        assert raw.commits == 1
    finally:
        for coroutine, _description in scheduled:
            coroutine.close()
