from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi import HTTPException
from tests.channel_submission_fixtures import channel_db, receipt_db

from app.dao.whatsapp_personal_dao import claim, get_personal, reaped, revoke
from app.db.errors import UniqueViolationError
from app.db.session import transaction
from app.records.outbound_plan import OutboundChunk, OutboundTarget
from app.records.whatsapp_personal import PersonalRoute
from app.services.channels.submissions import accept_channel_event
from app.services.channels.whatsapp_personal_control import start
from app.services.channels.whatsapp_personal_events import IdentityAmbiguousError, handle_frame
from app.services.channels.whatsapp_personal_intake import accept_message
from app.services.channels.whatsapp_personal_journal import (
    PersonalSendPendingError,
    PersonalSendTerminalError,
    authorize_dispatch,
    enqueue,
)
from app.services.channels.whatsapp_personal_protocol import Identity, Message, SendResult

__all__ = ["channel_db", "receipt_db"]


@pytest.mark.parametrize(
    ("account_id", "pn_jid", "lid_jid"),
    [
        ("999@lid", None, "999@lid"),
        ("15550002@s.whatsapp.net", "15550001@s.whatsapp.net", None),
        ("15550001:2@s.whatsapp.net", "15550001:2@s.whatsapp.net", None),
        ("15550001@s.whatsapp.net", "15550001@s.whatsapp.net", "999@s.whatsapp.net"),
    ],
)
async def test_invalid_identity_is_rejected_without_binding(channel_db, account_id, pn_jid, lid_jid):
    # Given a claimed pairing with no bound account.
    f = channel_db
    async with transaction(f.db.first):
        await start(f.config.agent_id, f.db.user.id, None)
        owner = await claim(f.config.id)
        assert owner is not None
    frame = Identity(type="identity", id="id", token="test", account_id=account_id, pn_jid=pn_jid, lid_jid=lid_jid)
    # When an invalid identity frame arrives.
    with pytest.raises(IdentityAmbiguousError):
        async with transaction(f.db.first):
            await handle_frame(owner, frame)
    # Then no account or connected state is persisted.
    async with transaction(f.db.second):
        row = await get_personal(f.config.agent_id)
        assert row is not None
        assert (row.account_id, row.pn_jid, row.lid_jid) == (None, None, None)
        assert row.state != "connected"


async def test_pn_identity_accepts_later_lid_enrichment(channel_db):
    # Given a claimed pairing and a PN-only identity.
    f = channel_db
    frame = Identity(
        type="identity",
        id="id",
        token="test",
        account_id="15550001@s.whatsapp.net",
        pn_jid="15550001@s.whatsapp.net",
        lid_jid=None,
    )
    async with transaction(f.db.first):
        await start(f.config.agent_id, f.db.user.id, None)
        owner = await claim(f.config.id)
        assert owner is not None
        await handle_frame(owner, frame)
    # When the SDK supplies the matching PN with a LID.
    async with transaction(f.db.first):
        await handle_frame(owner, frame.model_copy(update={"lid_jid": "999@lid"}))
    # Then the canonical account is unchanged and the LID is retained.
    async with transaction(f.db.second):
        row = await get_personal(f.config.agent_id)
        assert row is not None
        assert (row.account_id, row.pn_jid, row.lid_jid, row.state) == (
            "15550001@s.whatsapp.net",
            "15550001@s.whatsapp.net",
            "999@lid",
            "connected",
        )


async def test_canonical_pn_cannot_bind_two_agents(channel_db):
    # Given one linked account and another claimed agent.
    f = channel_db
    agent_id = uuid4()
    async with transaction(f.db.first):
        first = await connected(f)
        assert first.account_id is not None
        await f.db.first.execute(
            "INSERT INTO agents (id, name, creator_id, tenant_id, role_description, max_triggers, "
            "min_poll_interval_min, webhook_rate_limit, access_mode, company_access_level, heartbeat_interval_minutes) "
            "VALUES (%(id)s, 'Second', %(owner)s, %(tenant)s, '', 20, 5, 5, 'private', 'use', 240)",
            {"id": agent_id, "owner": f.db.user.id, "tenant": f.tenant_id},
        )
        await start(agent_id, f.db.user.id, None)
        second = await get_personal(agent_id)
        assert second is not None
        second = await claim(second.config_id)
        assert second is not None
    frame = Identity(
        type="identity", id="id", token="test", account_id=first.account_id, pn_jid=first.pn_jid, lid_jid=None
    )
    # When a different agent claims the same canonical PN without a LID.
    with pytest.raises(UniqueViolationError) as exc:
        async with transaction(f.db.second):
            await handle_frame(second, frame)
    # Then the global account reservation rejects it without binding the second agent.
    assert exc.value.constraint == "uq_whatsapp_personal_account"
    async with transaction(f.db.first):
        row = await get_personal(agent_id)
        assert row is not None
        assert row.account_id is None


async def test_owner_quota_failure_is_a_durable_rejection_without_guest_work(channel_db):
    # Given a linked owner with no remaining quota.
    f = channel_db
    async with transaction(f.db.first):
        owner = await connected(f)
        await f.db.first.execute("UPDATE users SET quota_message_limit=0 WHERE id=%(id)s", {"id": f.db.user.id})
    # When a valid new self-message reaches atomic submission.
    async with transaction(f.db.first):
        assert await accept_message(owner, message_for(owner))
    # Then the native event is terminally rejected: no charge, transcript or queued guest work, only a static notice.
    async with transaction(f.db.second):
        assert await f.db.second.fetchval("SELECT count(*) FROM channel_event_receipts") == 1
        assert await f.db.second.fetchval("SELECT outcome FROM personal_burst_inputs") == "quota_rejected"
        assert await f.db.second.fetchval("SELECT count(*) FROM chat_messages WHERE role='user'") == 0
        assert await f.db.second.fetchval("SELECT count(*) FROM gateway_messages WHERE status='pending'") == 0
        assert await f.db.second.fetchval("SELECT routing_reason FROM gateway_messages") == "whatsapp_busy"


async def test_config_delete_retains_account_reservation_and_nonresend_journal_until_reap(channel_db):
    # Given an unreaped socket and a durable pending command.
    f = channel_db
    async with transaction(f.db.first):
        owner = await connected(f)
        chunk = OutboundChunk(key=uuid4(), text="reserved reply")
        with pytest.raises(PersonalSendPendingError):
            await enqueue(target_for(owner), chunk)
    # When logical unlink is followed by config deletion.
    async with transaction(f.db.first):
        await revoke(owner.agent_id, purge=True)
        await f.db.first.execute("DELETE FROM channel_configs WHERE id=%(id)s", {"id": owner.config_id})
    # Then only the exact reaped owner can release the surviving account reservation.
    async with transaction(f.db.second):
        row = await get_personal(owner.agent_id)
        assert row is not None
        assert row.account_id == owner.account_id
        assert await f.db.second.fetchval("SELECT state FROM whatsapp_personal_sends") == "cancelled"
        assert not await reaped(replace(owner, claim_token=uuid4()))
        assert await reaped(owner)


async def connected(f):
    await start(f.config.agent_id, f.db.user.id, None)
    owner = await claim(f.config.id)
    assert owner is not None
    await handle_frame(
        owner,
        Identity(
            type="identity",
            id="id",
            token="test",
            account_id="15550001@s.whatsapp.net",
            pn_jid="15550001@s.whatsapp.net",
            lid_jid="999@lid",
        ),
    )
    result = await get_personal(f.config.agent_id)
    assert result is not None
    return result


def target_for(owner):
    route = PersonalRoute(
        config_id=owner.config_id,
        auth_epoch=owner.auth_epoch,
        generation=owner.generation,
        account_id=owner.account_id,
        self_jid=owner.pn_jid,
    )
    return OutboundTarget(
        provider="whatsapp",
        mode="personal_qr",
        config_id=owner.config_id,
        account=(owner.account_id, str(owner.auth_epoch), str(owner.generation)),
        destination=route.external_id(),
    )


def message_for(owner, native="native-1"):
    return Message(
        type="message",
        id="event",
        token="test",
        message_id=native,
        remote_jid=owner.pn_jid,
        participant=None,
        from_me=True,
        timestamp=int(datetime.now(UTC).timestamp()),
        text="self message",
        event_kind="notify",
        is_history=False,
    )


async def test_personal_self_text_is_atomic_and_replay_safe(channel_db):
    # Given a socket-owned self identity without a legacy member binding.
    f = channel_db
    async with transaction(f.db.first):
        owner = await connected(f)
        await f.db.first.execute("DELETE FROM org_members")
    # When the same native notification is replayed.
    async with transaction(f.db.first):
        assert await accept_message(owner, message_for(owner))
        assert await accept_message(owner, message_for(owner))
    # Then exactly one owner transcript, receipt and guest intent commit.
    async with transaction(f.db.second):
        assert await f.db.second.fetchval("SELECT count(*) FROM channel_event_receipts") == 1
        assert await f.db.second.fetchval("SELECT count(*) FROM gateway_messages") == 1
        assert await f.db.second.fetchval("SELECT sender_user_id FROM gateway_messages") == f.db.user.id


async def test_personal_rejects_other_conversations_and_legacy_intake(channel_db):
    # Given an active personal configuration.
    f = channel_db
    async with transaction(f.db.first):
        owner = await connected(f)
    # When a contact or Cloud notification tries to use it.
    async with transaction(f.db.first):
        assert not await accept_message(
            owner, message_for(owner).model_copy(update={"remote_jid": "123@s.whatsapp.net"})
        )
        with pytest.raises(HTTPException):
            await accept_channel_event(f.event)
    # Then neither path creates a receipt.
    async with transaction(f.db.second):
        assert await f.db.second.fetchval("SELECT count(*) FROM channel_event_receipts") == 0


async def test_journal_reap_makes_authorized_send_uncertain_never_resend(channel_db):
    # Given a durably reserved command.
    f = channel_db
    chunk = OutboundChunk(text="assistant", key=uuid4())
    async with transaction(f.db.first):
        owner = await connected(f)
        with pytest.raises(PersonalSendPendingError):
            await enqueue(target_for(owner), chunk)
    # When dispatch authorization commits and the child dies before ACK.
    async with transaction(f.db.first):
        command = await authorize_dispatch(owner)
        assert command is not None
    async with transaction(f.db.second):
        assert await reaped(owner)
    # Then polling is terminal and the account-scoped echo ID survives unlink.
    async with transaction(f.db.first):
        with pytest.raises(PersonalSendTerminalError):
            await enqueue(target_for(owner), chunk)
        await revoke(owner.agent_id, purge=True)
        assert await f.db.first.fetchval("SELECT message_id FROM whatsapp_personal_sends") == command.message_id
        assert await authorize_dispatch(owner) is None


async def test_journal_accepts_only_matching_owner_ack_and_suppresses_echo(channel_db):
    # Given an authorized command and its provider ID.
    f = channel_db
    chunk = OutboundChunk(text="assistant", key=uuid4())
    async with transaction(f.db.first):
        owner = await connected(f)
        with pytest.raises(PersonalSendPendingError):
            await enqueue(target_for(owner), chunk)
        command = await authorize_dispatch(owner)
        assert command is not None
    # When the exact owner reports the native server acknowledgement.
    async with transaction(f.db.second):
        await handle_frame(
            owner,
            SendResult(
                type="send_result",
                id="ack",
                token="test",
                command_id=str(command.command_id),
                message_id=command.message_id,
                outcome="accepted",
            ),
        )
    # Then worker retry succeeds without dispatch, and assistant echo is denied.
    async with transaction(f.db.first):
        await enqueue(target_for(owner), chunk)
        assert await authorize_dispatch(owner) is None
        with pytest.raises(HTTPException):
            await accept_message(owner, message_for(owner, command.message_id))


async def test_fresh_deactivation_denies_personal_intake(channel_db):
    # Given a socket that predates membership deactivation.
    f = channel_db
    async with transaction(f.db.first):
        owner = await connected(f)
        await f.db.first.execute("UPDATE users SET is_active=FALSE WHERE id=%(id)s", {"id": f.db.user.id})
    # When it submits new self text.
    async with transaction(f.db.first):
        with pytest.raises(HTTPException):
            await accept_message(owner, message_for(owner))
    # Then no quota or guest intent is charged.
    async with transaction(f.db.second):
        assert await f.db.second.fetchval("SELECT count(*) FROM gateway_messages") == 0
