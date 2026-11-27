from __future__ import annotations

import uuid
from unittest.mock import AsyncMock

import psycopg
import pytest
from fastapi import HTTPException

from app.dao import identity_dao, identity_provider_dao, org_member_dao, user_dao
from app.records.agent import AgentRecord
from app.records.chat import ChatMessageRecord, ChatSessionRecord
from app.records.chat_attachment import ChatAttachment
from app.records.identity import IdentityProviderRecord
from app.records.org import OrgMemberRecord
from app.records.organization_plan import OrganizationPlanId
from app.records.tenant import TenantRecord
from app.records.user import UserRecord
from app.services.channel_user_service import ChannelUserResolutionError, ChannelUserService
from app.services.channels import binding


def canonical_session(agent: AgentRecord, user: UserRecord) -> ChatSessionRecord:
    return ChatSessionRecord(
        id=uuid.uuid4(), agent_id=agent.id, user_id=user.id, source_channel="web", is_primary=True, is_group=False
    )


@pytest.fixture
def context(monkeypatch):
    tenant_id = uuid.uuid4()
    user = UserRecord(id=uuid.uuid4(), tenant_id=tenant_id)
    agent = AgentRecord(id=uuid.uuid4(), creator_id=uuid.uuid4(), tenant_id=tenant_id, name="Bot")
    provider = IdentityProviderRecord(id=uuid.uuid4(), provider_type="slack", name="Slack", tenant_id=tenant_id)
    member = OrgMemberRecord(id=uuid.uuid4(), provider_id=provider.id, user_id=user.id, name="Human")
    canonical = canonical_session(agent, user)
    monkeypatch.setattr(binding.identity_provider_dao, "get_by_type_and_tenant", AsyncMock(return_value=provider))
    monkeypatch.setattr(binding.channel_user_service, "_find_org_member", AsyncMock(return_value=member))
    monkeypatch.setattr(binding.user_dao, "get_with_identity", AsyncMock(return_value=user))
    monkeypatch.setattr(binding, "load_user_principal", AsyncMock(return_value=user))
    monkeypatch.setattr(binding, "check_agent_access", AsyncMock(return_value=(agent, "use")))
    monkeypatch.setattr(
        binding.company_admission_dao,
        "tenant",
        AsyncMock(return_value=TenantRecord(tenant_id, "Company", "company", tier=OrganizationPlanId.PRO)),
    )
    monkeypatch.setattr(binding, "ensure_canonical_session", AsyncMock(return_value=canonical))
    return agent, user, canonical


@pytest.mark.parametrize(
    "source",
    ["slack", "feishu", "dingtalk", "google_chat", "microsoft_teams", "wecom", "whatsapp", "discord", "wechat"],
)
async def test_direct_binding_separates_transcript_from_provider_address(context, source):
    # Given
    agent, user, canonical = context
    provider = ChatSessionRecord(
        id=uuid.uuid4(), agent_id=agent.id, user_id=user.id, source_channel=source, external_conv_id="provider-address"
    )
    # When
    conversation = await binding.bind_channel_conversation(agent=agent, platform_user=user, reply_session=provider)
    # Then the sender's own canonical session is the transcript and the provider row stays the reply address.
    assert conversation.transcript_session is canonical
    assert conversation.reply_session is provider
    assert provider.source_channel == source
    assert provider.external_conv_id == "provider-address"
    assert isinstance(binding.ensure_canonical_session, AsyncMock)
    binding.ensure_canonical_session.assert_awaited_once_with(agent.id, user.id)
    assert isinstance(binding.load_user_principal, AsyncMock)
    assert isinstance(binding.check_agent_access, AsyncMock)
    binding.load_user_principal.assert_awaited_once_with(user.id, fresh=True)
    binding.check_agent_access.assert_awaited_once_with(user, agent.id, fresh=True)


async def test_two_linked_senders_bind_to_their_own_canonical_sessions(context, monkeypatch):
    # Given two linked members of one agent.
    agent, user, canonical = context
    other = UserRecord(id=uuid.uuid4(), tenant_id=user.tenant_id)
    other_canonical = canonical_session(agent, other)
    rows = {user.id: canonical, other.id: other_canonical}
    principals = {user.id: user, other.id: other}
    monkeypatch.setattr(binding, "load_user_principal", AsyncMock(side_effect=lambda uid, *, fresh: principals[uid]))
    monkeypatch.setattr(binding, "ensure_canonical_session", AsyncMock(side_effect=lambda _agent, uid: rows[uid]))
    first_route = ChatSessionRecord(id=uuid.uuid4(), agent_id=agent.id, user_id=user.id, source_channel="slack")
    second_route = ChatSessionRecord(id=uuid.uuid4(), agent_id=agent.id, user_id=other.id, source_channel="slack")
    # When each sender is bound.
    first = await binding.bind_channel_conversation(agent=agent, platform_user=user, reply_session=first_route)
    second = await binding.bind_channel_conversation(agent=agent, platform_user=other, reply_session=second_route)
    # Then the transcripts are two different rows, each owned by its sender, and neither is the agent creator's.
    assert first.transcript_session is canonical
    assert second.transcript_session is other_canonical
    assert first.transcript_session.id != second.transcript_session.id
    assert {first.transcript_session.user_id, second.transcript_session.user_id} == {user.id, other.id}
    assert isinstance(binding.ensure_canonical_session, AsyncMock)
    assert [call.args for call in binding.ensure_canonical_session.await_args_list] == [
        (agent.id, user.id),
        (agent.id, other.id),
    ]


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
    assert isinstance(binding.ensure_canonical_session, AsyncMock)
    binding.ensure_canonical_session.assert_not_awaited()


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
    assert isinstance(binding.ensure_canonical_session, AsyncMock)
    binding.ensure_canonical_session.assert_not_awaited()


@pytest.mark.parametrize("rejection", ["principal", "tenant", "access"])
async def test_denied_direct_sender_cannot_open_a_canonical_session(context, monkeypatch, rejection):
    # Given
    agent, user, _ = context
    route = ChatSessionRecord(id=uuid.uuid4(), agent_id=agent.id, user_id=user.id, source_channel="slack")
    assert isinstance(binding.load_user_principal, AsyncMock)
    assert isinstance(binding.check_agent_access, AsyncMock)
    assert isinstance(binding.ensure_canonical_session, AsyncMock)
    if rejection == "principal":
        binding.load_user_principal.side_effect = HTTPException(403)
    elif rejection == "tenant":
        monkeypatch.setattr(binding.company_admission_dao, "tenant", AsyncMock(return_value=None))
    else:
        binding.check_agent_access.side_effect = HTTPException(403)
    # When / Then
    with pytest.raises((HTTPException, ChannelUserResolutionError)):
        await binding.bind_channel_conversation(agent=agent, platform_user=user, reply_session=route)
    binding.ensure_canonical_session.assert_not_awaited()


@pytest.mark.parametrize(
    ("channel_type", "external_user_id", "extra_info"),
    [
        ("slack", "unknown", {}),
        ("feishu", None, {"open_id": "ou_open_123"}),
        ("wechat", "wx_user_123", {"external_id": "wx_user_123"}),
    ],
)
async def test_unbound_external_sender_never_uses_creator_or_lazy_identity(
    context, monkeypatch, channel_type, external_user_id, extra_info
):
    # Given a sender with no linked member, and every write that could register an identity, user or member for it
    agent, _, _ = context
    monkeypatch.setattr(binding.channel_user_service, "_find_org_member", AsyncMock(return_value=None))
    creators = [AsyncMock() for _ in range(5)]
    writes = [
        (identity_dao, "create"),
        (identity_dao, "create_identity"),
        (user_dao, "create"),
        (org_member_dao, "create"),
        (identity_provider_dao, "create"),
    ]
    for (dao, name), creator in zip(writes, creators, strict=True):
        monkeypatch.setattr(dao, name, creator)
    # When / Then
    with pytest.raises(ChannelUserResolutionError):
        await binding.resolve_bound_sender(
            agent=agent, channel_type=channel_type, external_user_id=external_user_id, extra_info=extra_info
        )
    for creator in creators:
        creator.assert_not_awaited()
    assert isinstance(binding.ensure_canonical_session, AsyncMock)
    binding.ensure_canonical_session.assert_not_awaited()


async def test_a_failed_member_lookup_raises_instead_of_reading_as_an_unlinked_sender(context, monkeypatch):
    # Given the member lookup itself fails, as it does when the database is unreachable
    agent, _, _ = context
    monkeypatch.setattr(binding.channel_user_service, "_find_org_member", ChannelUserService()._find_org_member)
    lost = psycopg.OperationalError("connection lost")
    monkeypatch.setattr(org_member_dao, "find_active_by_any_ids", AsyncMock(side_effect=lost))
    # When / Then the failure reaches the delivery, which fails so the provider retries, instead of being acknowledged
    with pytest.raises(psycopg.OperationalError):
        await binding.resolve_bound_sender(agent=agent, channel_type="slack", external_user_id="U123", extra_info={})


@pytest.fixture
def persisted(context, monkeypatch):
    """Run `persist_channel_user_message` against a fake pool, recording who each scheduled publication targets."""
    from app.db import session as db_session
    from test_db_session import _FakePool, _FakeRawConnection

    agent, user, _ = context
    raw = _FakeRawConnection()
    monkeypatch.setattr(db_session, "get_pool", lambda: _FakePool(raw))
    published = AsyncMock()
    monkeypatch.setattr(binding, "_publish_user_message", published)
    scheduled = []

    def schedule(coroutine, description):
        db_session.run_after_commit(lambda: scheduled.append((coroutine, description)), coroutine.close)

    monkeypatch.setattr("app.api.background_tasks.schedule_background_task", schedule)

    async def persist(session: ChatSessionRecord, attachments: tuple[ChatAttachment, ...] = ()):
        message = ChatMessageRecord(
            id=uuid.uuid4(),
            agent_id=agent.id,
            user_id=user.id,
            role="user",
            content="hello",
            conversation_id=str(session.id),
        )
        monkeypatch.setattr(binding.chat_message_dao, "insert_message", AsyncMock(return_value=message))
        monkeypatch.setattr(binding.chat_message_dao, "create", AsyncMock(return_value=message))
        monkeypatch.setattr(binding.chat_session_dao, "get", AsyncMock(return_value=session))
        monkeypatch.setattr(binding.chat_session_dao, "update", AsyncMock())
        async with db_session.connection_ctx():
            _ = await binding.persist_channel_user_message(
                agent_id=agent.id,
                user_id=user.id,
                conversation_id=str(session.id),
                content="hello",
                attachments=attachments,
            )
            assert scheduled == []
        assert raw.commits == 1

    try:
        yield persist, published, scheduled
    finally:
        for coroutine, _description in scheduled:
            coroutine.close()


async def test_canonical_user_event_is_deferred_until_commit_and_filtered_to_its_owner(context, persisted):
    # Given the sender's canonical session.
    _, user, canonical = context
    persist, published, scheduled = persisted
    # When a user message is persisted.
    await persist(canonical)
    # Then the realtime event is scheduled once, after the commit, for the owner's sockets only.
    assert len(scheduled) == 1
    published.assert_called_once()
    assert published.call_args.args[1] == str(canonical.id)
    assert published.call_args.args[3] == str(user.id)


@pytest.mark.parametrize("with_attachment", [True, False])
async def test_group_event_publishes_only_attachments_and_to_every_viewer(context, persisted, with_attachment):
    # Given a group session, whose transcript every member of the group may watch.
    agent, user, _ = context
    persist, published, scheduled = persisted
    group = ChatSessionRecord(
        id=uuid.uuid4(), agent_id=agent.id, user_id=user.id, source_channel="slack", is_group=True
    )
    attachments = (ChatAttachment(path="workspace/uploads/a.txt", name="a.txt"),) if with_attachment else ()
    # When a user message is persisted.
    await persist(group, attachments)
    # Then only a message with an attachment is published, unfiltered by user.
    assert len(scheduled) == int(with_attachment)
    if with_attachment:
        assert published.call_args.args[3] is None
    else:
        published.assert_not_called()
