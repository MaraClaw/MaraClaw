from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import uuid4

import pytest
from tests.channel_submission_fixtures import ChannelDatabase
from tests.test_zalo_media_plan import stored_file
from tests.zalo_personal_fixtures import bind, channel_db, connected, receipt_db, route_for

from app.dao.zalo_personal_dao import claim, get_personal, reaped, revoke
from app.dao.zalo_personal_runtime_dao import interrupted, runtime_for
from app.db.session import transaction
from app.records.outbound_plan import OutboundChunk, OutboundFileChunk
from app.services.channels.whatsapp_personal_journal import PersonalSendPendingError, PersonalSendTerminalError
from app.services.channels.zalo_personal_journal import authorize_dispatch, enqueue, record_result
from app.services.channels.zalo_personal_media_journal import authorize_media_dispatch, pending_media, reject_media

__all__ = ["channel_db", "receipt_db"]


@pytest.mark.parametrize("outcome", ["accepted", "rejected", "uncertain"])
async def test_media_staging_is_unclaimed_and_results_are_durable(channel_db: ChannelDatabase, outcome: str) -> None:
    f, chunk = channel_db, OutboundFileChunk(key=uuid4(), file=stored_file())
    async with transaction(f.db.first):
        owned = await connected(f)
        await bind(f, owned)
        destination = route_for(owned).external_id()
        for _ in range(2):
            with pytest.raises(PersonalSendPendingError):
                await enqueue(destination, chunk)
        queued = await pending_media(owned)
        assert queued is not None
        assert queued.command_id == chunk.key
        assert queued.state == "pending"
        assert queued.claim_token is None
        assert queued.dispatched_at is None
        assert await pending_media(owned) == queued
        assert await authorize_media_dispatch(owned, uuid4()) is None
        dispatched = await authorize_media_dispatch(owned, chunk.key)
        assert dispatched is not None
        assert dispatched.state == "dispatch_started"
        assert dispatched.claim_token == owned.claim_token
        assert dispatched.dispatched_at is not None
        assert await authorize_media_dispatch(owned, chunk.key) is None
        await record_result(owned, chunk.key, outcome, "attachment-1" if outcome == "accepted" else "")
        if outcome == "accepted":
            await enqueue(destination, chunk)
            assert (await runtime_for(owned.config_id)).last_activity_at is not None
        else:
            with pytest.raises(PersonalSendTerminalError, match="personal_send_" + outcome):
                await enqueue(destination, chunk)
        assert await f.db.first.fetchval("SELECT count(*) FROM zalo_personal_media_sends") == 1
        assert await f.db.first.fetchval("SELECT count(*) FROM zalo_personal_sends") == 0


async def test_media_payload_conflicts_and_cross_kind_ids_are_terminal(channel_db: ChannelDatabase) -> None:
    f, chunk = channel_db, OutboundFileChunk(key=uuid4(), file=stored_file())
    async with transaction(f.db.first):
        owned = await connected(f)
        await bind(f, owned)
        route = route_for(owned).external_id()
        with pytest.raises(PersonalSendPendingError):
            await enqueue(route, chunk)
        with pytest.raises(PersonalSendTerminalError, match="personal_target_changed"):
            await enqueue(route, chunk.model_copy(update={"file": chunk.file.model_copy(update={"sha256": "b" * 64})}))
        with pytest.raises(PersonalSendTerminalError, match="personal_target_changed"):
            await enqueue(route, OutboundChunk(key=chunk.key, text="different kind"))
        text = OutboundChunk(key=uuid4(), text="text first")
        with pytest.raises(PersonalSendPendingError):
            await enqueue(route, text)
        with pytest.raises(PersonalSendTerminalError, match="personal_target_changed"):
            await enqueue(route, chunk.model_copy(update={"key": text.key}))
        text_send = await authorize_dispatch(owned)
        media_send = await authorize_media_dispatch(owned, chunk.key)
        assert text_send is not None
        assert text_send.command_id == text.key
        assert media_send is not None
        assert media_send.command_id == chunk.key


@pytest.mark.parametrize("transition", ["unbound", "revoke", "reject", "reap", "expiry"])
async def test_media_terminal_transitions_never_dispatch_again(
    channel_db: ChannelDatabase, transition: Literal["unbound", "revoke", "reject", "reap", "expiry"]
) -> None:
    f, chunk = channel_db, OutboundFileChunk(key=uuid4(), file=stored_file())
    async with transaction(f.db.first):
        owned = await connected(f)
        await bind(f, owned)
        with pytest.raises(PersonalSendPendingError):
            await enqueue(route_for(owned).external_id(), chunk)
        match transition:
            case "unbound":
                await f.db.first.execute("DELETE FROM channel_sender_bindings")
                assert await authorize_media_dispatch(owned, chunk.key) is None
                expected = "cancelled"
            case "revoke":
                await revoke(owned.agent_id, purge=True)
                retained = await get_personal(owned.agent_id)
                assert retained is not None
                assert retained.account_id == owned.account_id
                assert retained.claim_token == owned.claim_token
                assert await reaped(owned)
                released = await get_personal(owned.agent_id)
                assert released is not None
                assert released.account_id is None
                expected = "cancelled"
            case "reject":
                await reject_media(owned, chunk.key)
                assert await authorize_media_dispatch(owned, chunk.key) is None
                expected = "rejected"
            case "reap":
                assert await authorize_media_dispatch(owned, chunk.key) is not None
                assert await reaped(owned)
                expected = "uncertain"
            case "expiry":
                assert await authorize_media_dispatch(owned, chunk.key) is not None
                await f.db.first.execute(
                    "UPDATE zalo_personal_media_sends SET dispatched_at=now()-interval '121 seconds'"
                )
                assert await pending_media(owned) is None
                assert await f.db.first.fetchval("SELECT state FROM zalo_personal_media_sends") == "dispatch_started"
                await f.db.first.execute(
                    "UPDATE zalo_personal_media_sends SET dispatched_at=now()-interval '136 seconds'"
                )
                assert await pending_media(owned) is None
                assert await authorize_media_dispatch(owned, chunk.key) is None
                expected = "uncertain"
        assert await f.db.first.fetchval("SELECT state FROM zalo_personal_media_sends") == expected


async def test_media_pending_survives_all_five_durable_backoffs(channel_db: ChannelDatabase) -> None:
    f, chunk = channel_db, OutboundFileChunk(key=uuid4(), file=stored_file())
    async with transaction(f.db.first):
        owned = await connected(f)
        await bind(f, owned)
        destination = route_for(owned).external_id()
        with pytest.raises(PersonalSendPendingError):
            await enqueue(destination, chunk)
        for delay in (5, 15, 30, 60, 120):
            assert await interrupted(owned)
            runtime = await runtime_for(owned.config_id)
            assert runtime.retry_at is not None
            assert delay - 1 <= (runtime.retry_at - datetime.now(UTC)).total_seconds() <= delay
            assert await reaped(owned)
            with pytest.raises(PersonalSendPendingError):
                await enqueue(destination, chunk)
            assert await claim(owned.config_id) is None
            await f.db.first.execute(
                "UPDATE channel_configs SET extra_config=jsonb_set(extra_config::jsonb,"
                "'{zalo_runtime,retry_at}',to_jsonb(%(past)s::text))::json WHERE id=%(id)s",
                {"id": owned.config_id, "past": (datetime.now(UTC) - timedelta(seconds=1)).isoformat()},
            )
            recovered = await claim(owned.config_id)
            assert recovered is not None
            await f.db.first.execute("UPDATE zalo_personal_connections SET state='connected'")
            owned = replace(recovered, state="connected")
            queued = await pending_media(owned)
            assert queued is not None
            assert queued.command_id == chunk.key
            assert queued.file == chunk.file
        assert await authorize_media_dispatch(owned, chunk.key) is not None
