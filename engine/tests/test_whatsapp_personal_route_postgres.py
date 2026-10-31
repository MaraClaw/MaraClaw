from collections.abc import Mapping
from dataclasses import replace
from uuid import UUID, uuid4

import pytest
from tests.channel_submission_fixtures import channel_db, receipt_db
from tests.test_whatsapp_personal_integration import connected, message_for, target_for

from app.db.session import transaction
from app.records.chat import ChatSessionRecord
from app.records.outbound_plan import OutboundChunk
from app.records.whatsapp_personal import PersonalConnection, PersonalRoute, PersonalSource
from app.services.channels.outbound_plan import configured_outbound
from app.services.channels.submissions import ChannelEvent, is_channel_event_replay
from app.services.channels.whatsapp_personal_intake import accept_message
from app.services.channels.whatsapp_personal_journal import (
    PersonalSendPendingError,
    PersonalSendTerminalError,
    enqueue,
)

__all__ = ["channel_db", "receipt_db"]

_DEPLOYED_BOUND = 200


@pytest.fixture
async def deployed_channel_db(channel_db):
    # Model the deployed catalog: chat_sessions.external_conv_id is VARCHAR(200), not the fresh baseline TEXT.
    async with transaction(channel_db.db.first):
        await channel_db.db.first.execute("ALTER TABLE chat_sessions ALTER COLUMN external_conv_id TYPE VARCHAR(200)")
    return channel_db


async def test_personal_intake_fits_deployed_conversation_bound(deployed_channel_db):
    # Given a connected personal socket on the deployed VARCHAR(200) session column.
    f = deployed_channel_db
    async with transaction(f.db.first):
        owner = await connected(f)
    expected = PersonalRoute(
        config_id=owner.config_id,
        auth_epoch=owner.auth_epoch,
        generation=owner.generation,
        account_id=str(owner.account_id),
        self_jid=str(owner.pn_jid),
    ).external_id()
    # When the actual intake accepts a new self message and then replays it.
    async with transaction(f.db.first):
        assert await accept_message(owner, message_for(owner))
        assert await accept_message(owner, message_for(owner))
    # Then one receipt, quota charge, transcript, guest intent and reply intent commit under the bound.
    async with transaction(f.db.second):
        db = f.db.second
        session = await db.fetchone(
            "SELECT id, external_conv_id, source_channel FROM chat_sessions WHERE external_conv_id IS NOT NULL"
        )
        assert session is not None
        assert session["external_conv_id"] == expected
        assert len(expected) <= _DEPLOYED_BOUND
        assert session["source_channel"] == "whatsapp"
        assert await db.fetchval("SELECT conversation_id FROM channel_event_receipts") == expected
        assert await db.fetchval("SELECT count(*) FROM channel_event_receipts") == 1
        assert await db.fetchval("SELECT quota_messages_used FROM users WHERE id=%(id)s", {"id": f.db.user.id}) == 1
        assert await db.fetchval("SELECT count(*) FROM chat_messages WHERE role='user'") == 1
        guest = await db.fetchone("SELECT sender_user_id, conversation_id FROM gateway_messages")
        assert guest is not None
        assert guest["sender_user_id"] == f.db.user.id
        reply = await db.fetchone("SELECT destination_kind, destination_id FROM gateway_report_deliveries")
        assert reply is not None
        assert (reply["destination_kind"], UUID(str(reply["destination_id"]))) == ("im", UUID(str(session["id"])))


def route_of(owner: PersonalConnection, stale: Mapping[str, UUID | str] | None = None) -> PersonalRoute:
    current = PersonalRoute(
        config_id=owner.config_id,
        auth_epoch=owner.auth_epoch,
        generation=owner.generation,
        account_id=str(owner.account_id),
        self_jid=str(owner.pn_jid),
    )
    return current.model_copy(update=dict(stale or {}))


def session_for(f, external: str) -> ChatSessionRecord:
    return ChatSessionRecord(
        id=uuid4(), agent_id=f.db.agent.id, user_id=f.db.user.id, source_channel="whatsapp", external_conv_id=external
    )


def legacy_external(route: PersonalRoute) -> str:
    return "whatsapp_personal:" + route.model_dump_json()


async def test_replay_matches_across_legacy_and_compact_conversation_forms(deployed_channel_db):
    # Given a message accepted under the compact conversation id.
    f = deployed_channel_db
    async with transaction(f.db.first):
        owner = await connected(f)
        assert await accept_message(owner, message_for(owner))
    assert owner.claim_token is not None
    source = PersonalSource(
        owner.config_id, owner.auth_epoch, owner.generation, owner.claim_token, str(owner.account_id), "native-1"
    )
    event = ChannelEvent(
        config_id=owner.config_id,
        agent_id=owner.agent_id,
        provider="whatsapp",
        event_key=f"personal:{owner.auth_epoch}:native-1",
        sender_id=str(owner.account_id),
        conversation_id=legacy_external(route_of(owner)),
        text="self message",
        personal_source=source,
    )
    # When the same native event is presented with the earlier prefixed-JSON conversation id.
    async with transaction(f.db.second):
        replayed = await is_channel_event_replay(event)
    # Then the unchanged auth-epoch fingerprint still identifies it as a replay.
    assert replayed
    assert event.fingerprint() == replace(event, conversation_id=route_of(owner).external_id()).fingerprint()


@pytest.mark.parametrize("form", ["compact", "legacy"])
async def test_outbound_and_journal_accept_both_persisted_route_forms(deployed_channel_db, form):
    # Given a connected socket and a persisted session in either route form.
    f = deployed_channel_db
    async with transaction(f.db.first):
        owner = await connected(f)
    route = route_of(owner)
    external = route.external_id() if form == "compact" else legacy_external(route)
    chunk = OutboundChunk(text="assistant", key=uuid4())
    # When the outbound target is planned and journaled.
    async with transaction(f.db.first):
        planned = await configured_outbound(f.db.agent, session_for(f, external))
        with pytest.raises(PersonalSendPendingError):
            await enqueue(planned.target, chunk)
    # Then the original destination bytes are kept and the journal holds every routing fence exactly.
    assert planned.target.destination == external
    assert planned.target.account == (str(owner.account_id), str(owner.auth_epoch), str(owner.generation))
    async with transaction(f.db.second):
        sent = await f.db.second.fetchone(
            "SELECT config_id, auth_epoch, generation, account_id, self_jid FROM whatsapp_personal_sends"
        )
    assert sent is not None
    assert (sent["config_id"], sent["auth_epoch"], sent["generation"], sent["account_id"], sent["self_jid"]) == (
        owner.config_id,
        owner.auth_epoch,
        owner.generation,
        owner.account_id,
        owner.pn_jid,
    )


@pytest.mark.parametrize("form", ["compact", "legacy"])
@pytest.mark.parametrize(
    "stale",
    [
        {"config_id": uuid4()},
        {"auth_epoch": uuid4()},
        {"generation": uuid4()},
        {"account_id": "15550009@s.whatsapp.net"},
        {"self_jid": "15550009@s.whatsapp.net"},
    ],
)
async def test_stale_route_fence_is_revoked_not_retargeted(deployed_channel_db, form, stale):
    # Given a connected socket and a persisted route whose one fence no longer matches it.
    f = deployed_channel_db
    async with transaction(f.db.first):
        owner = await connected(f)
    route = route_of(owner, stale)
    external = route.external_id() if form == "compact" else legacy_external(route)
    # When outbound planning resolves it.
    async with transaction(f.db.first):
        with pytest.raises(PersonalSendTerminalError) as exc:
            await configured_outbound(f.db.agent, session_for(f, external))
    # Then the route is revoked and nothing is journaled.
    assert exc.value.code == "personal_route_revoked"
    async with transaction(f.db.second):
        assert await f.db.second.fetchval("SELECT count(*) FROM whatsapp_personal_sends") == 0


@pytest.mark.parametrize("suffix", ["2:" + "0" * 32 + ":" + "1" * 32 + ":" + "2" * 32 + ":a:b", "1:not-hex", "{bad"])
async def test_invalid_route_is_terminal_in_planning_and_journal(deployed_channel_db, suffix):
    # Given a connected socket and an undecodable persisted route.
    f = deployed_channel_db
    async with transaction(f.db.first):
        owner = await connected(f)
    external = "whatsapp_personal:" + suffix
    # When planning and the journal parse it.
    async with transaction(f.db.first):
        with pytest.raises(PersonalSendTerminalError) as planning:
            await configured_outbound(f.db.agent, session_for(f, external))
        with pytest.raises(PersonalSendTerminalError) as journal:
            await enqueue(
                target_for(owner).model_copy(update={"destination": external}), OutboundChunk(text="x", key=uuid4())
            )
    # Then both stop terminally instead of retrying.
    assert (planning.value.code, journal.value.code) == ("personal_route_invalid", "personal_route_invalid")
