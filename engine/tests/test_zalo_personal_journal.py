from uuid import uuid4

import pytest
from tests.zalo_personal_fixtures import bind, channel_db, connected, receipt_db, route_for

from app.dao.zalo_personal_dao import reaped
from app.db.session import transaction
from app.records.outbound_plan import OutboundChunk
from app.services.channels.whatsapp_personal_journal import PersonalSendPendingError, PersonalSendTerminalError
from app.services.channels.zalo_personal_journal import authorize_dispatch, enqueue, record_result

__all__ = ["channel_db", "receipt_db"]


@pytest.mark.parametrize("outcome", ["accepted", "rejected", "uncertain"])
async def test_journal_idempotent_outcomes_and_conflicts(channel_db, outcome):
    f = channel_db
    chunk = OutboundChunk(key=uuid4(), text="reply")
    async with transaction(f.db.first):
        owned = await connected(f)
        await bind(f, owned)
        destination = route_for(owned).external_id()
        with pytest.raises(PersonalSendPendingError):
            await enqueue(destination, chunk)
        with pytest.raises(PersonalSendPendingError):
            await enqueue(destination, chunk)
        with pytest.raises(PersonalSendTerminalError, match="personal_target_changed"):
            await enqueue(destination, OutboundChunk(key=chunk.key, text="changed"))
        command = await authorize_dispatch(owned)
        assert command is not None
        assert command.command_id == chunk.key
        assert await authorize_dispatch(owned) is None
        await record_result(owned, chunk.key, outcome, "native-send" if outcome == "accepted" else "")
        if outcome == "accepted":
            await enqueue(destination, chunk)
        else:
            with pytest.raises(PersonalSendTerminalError, match="personal_send_" + outcome):
                await enqueue(destination, chunk)
        assert await f.db.first.fetchval("SELECT count(*) FROM zalo_personal_sends") == 1


async def test_unbound_peer_cannot_queue_and_unlink_cancels_before_dispatch(channel_db):
    f = channel_db
    async with transaction(f.db.first):
        owned = await connected(f)
        destination = route_for(owned).external_id()
        with pytest.raises(PersonalSendTerminalError, match="zalo_peer_unbound"):
            await enqueue(destination, OutboundChunk(key=uuid4(), text="unbound"))
        await bind(f, owned)
        with pytest.raises(PersonalSendPendingError):
            await enqueue(destination, OutboundChunk(key=uuid4(), text="bound"))
        await f.db.first.execute("DELETE FROM channel_sender_bindings")
        assert await authorize_dispatch(owned) is None
        assert await f.db.first.fetchval("SELECT state FROM zalo_personal_sends") == "cancelled"


async def test_reap_leaves_started_send_uncertain_and_never_retries(channel_db):
    f = channel_db
    async with transaction(f.db.first):
        owned = await connected(f)
        await bind(f, owned)
        with pytest.raises(PersonalSendPendingError):
            await enqueue(route_for(owned).external_id(), OutboundChunk(key=uuid4(), text="in flight"))
        assert await authorize_dispatch(owned) is not None
        assert await reaped(owned)
        assert await f.db.first.fetchval("SELECT state FROM zalo_personal_sends") == "uncertain"
