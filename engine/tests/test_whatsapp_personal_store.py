from datetime import timedelta
from uuid import uuid4

import pytest
from cryptography.exceptions import InvalidTag
from fastapi import HTTPException
from tests.channel_submission_fixtures import channel_db, receipt_db
from tests.test_whatsapp_personal_integration import connected, target_for

from app.dao.whatsapp_personal_auth_dao import AuthMutation, read_auth, write_auth
from app.dao.whatsapp_personal_dao import claim, get_personal, reaped, renew
from app.dao.whatsapp_personal_runtime_dao import RETRY_DELAYS, runtime_for
from app.db.session import transaction
from app.records.outbound_plan import OutboundChunk
from app.records.whatsapp_personal import PersonalRoute
from app.services.channels.whatsapp_personal_control import disconnect, start
from app.services.channels.whatsapp_personal_crypto import AuthCipher, AuthKey
from app.services.channels.whatsapp_personal_events import handle_frame, interrupted
from app.services.channels.whatsapp_personal_journal import (
    PersonalSendPendingError,
    authorize_dispatch,
    enqueue,
    route_matches,
)
from app.services.channels.whatsapp_personal_protocol import Identity

__all__ = ["channel_db", "receipt_db"]


async def test_expired_lease_never_steals_unreaped_child(channel_db):
    # Given an expired claim with physical death unproven.
    f = channel_db
    async with transaction(f.db.first):
        await start(f.config.agent_id, f.db.user.id, None)
        row = await get_personal(f.config.agent_id)
        assert row is not None
        owned = await claim(row.config_id)
        assert owned is not None
        await f.db.first.execute("UPDATE whatsapp_personal_connections SET lease_until=now()-interval '1 day'")
    # When another connector tries to claim it.
    async with transaction(f.db.second):
        stolen = await claim(row.config_id)
    # Then expiry cannot attest socket death or permit takeover.
    assert stolen is None


async def test_auth_roundtrip_is_fenced_and_aad_bound(channel_db):
    # Given an eligible claimed session.
    f = channel_db
    async with transaction(f.db.first):
        await start(f.config.agent_id, f.db.user.id, None)
        row = await get_personal(f.config.agent_id)
        assert row is not None
        owned = await claim(row.config_id)
        assert owned is not None
        address = AuthKey(row.config_id, row.auth_epoch, "sender-key", "key-1")
        await write_auth(owned, AuthMutation("sender-key", {"key-1": {"type": "Buffer", "data": [1, 2]}}))
    # When credentials are read through a second transaction.
    async with transaction(f.db.second):
        values = await read_auth(owned, "sender-key", ["key-1"])
        stored = await f.db.second.fetchone("SELECT ciphertext FROM whatsapp_personal_auth")
    # Then only the claimed epoch decrypts, never plaintext storage.
    import json

    assert json.loads(values["key-1"]) == {"type": "Buffer", "data": [1, 2]}
    assert stored is not None
    assert values["key-1"].encode() not in bytes(stored["ciphertext"])
    cipher = AuthCipher("test-key")
    ciphertext = cipher.seal(address, "secret")
    with pytest.raises(InvalidTag):
        cipher.open(AuthKey(row.config_id, uuid4(), "sender-key", "key-1"), ciphertext)


async def test_unlink_fences_now_but_releases_account_only_after_exact_reap(channel_db):
    # Given a bound live owner.
    f = channel_db
    async with transaction(f.db.first):
        await start(f.config.agent_id, f.db.user.id, None)
        row = await get_personal(f.config.agent_id)
        assert row is not None
        owned = await claim(row.config_id)
        assert owned is not None
        await f.db.first.execute("UPDATE whatsapp_personal_connections SET account_id='15550001@s.whatsapp.net'")
        await write_auth(owned, AuthMutation("creds", {"main": "secret"}))
    # When the creator unlinks.
    async with transaction(f.db.second):
        stopped = await disconnect(f.config.agent_id, f.db.user.id, row.generation)
        retained = await get_personal(f.config.agent_id)
        assert await f.db.second.fetchval("SELECT count(*) FROM whatsapp_personal_auth") == 0
    # Then the route is revoked but the old socket still reserves its account.
    assert stopped.state == "disconnecting"
    assert retained is not None
    assert retained.account_id == "15550001@s.whatsapp.net"
    async with transaction(f.db.first):
        assert not await renew(owned)
        assert await reaped(owned)
        released = await get_personal(f.config.agent_id)
    assert released is not None
    assert released.account_id is None
    assert released.state == "disconnected"


async def test_start_rejects_stale_generation_and_noncreator(channel_db):
    # Given a configured personal attempt.
    f = channel_db
    async with transaction(f.db.first):
        await start(f.config.agent_id, f.db.user.id, None)
        row = await get_personal(f.config.agent_id)
        assert row is not None
    # When another membership or stale browser tries to replace it.
    async with transaction(f.db.second):
        with pytest.raises(HTTPException) as denied:
            await start(f.config.agent_id, f.sender_id, row.generation)
        assert denied.value.status_code == 403
        with pytest.raises(HTTPException) as stale:
            await start(f.config.agent_id, f.db.user.id, uuid4())
        assert stale.value.status_code == 409
    # Then the pairing deadline is fixed and no replacement occurred.
    assert row.expires_at is not None
    assert row.expires_at - row.created_at <= timedelta(seconds=121)


async def paired(f):
    owner = await connected(f)
    await write_auth(owner, AuthMutation("creds", {"main": "secret"}))
    return owner


async def test_exhausted_retry_budget_pauses_with_credentials_kept(channel_db):
    # Given a paired connection with one pending reply.
    f = channel_db
    async with transaction(f.db.first):
        owner = await paired(f)
        chunk = OutboundChunk(text="synthetic", key=uuid4())
        with pytest.raises(PersonalSendPendingError):
            await enqueue(target_for(owner), chunk)
    # When transient failures spend the whole retry budget.
    for _ in RETRY_DELAYS:
        async with transaction(f.db.first):
            assert await interrupted(owner)
    async with transaction(f.db.second):
        retrying = await get_personal(owner.agent_id)
        spent = await runtime_for(owner.config_id)
    assert retrying is not None
    assert retrying.state == "reconnecting"
    assert retrying.desired
    assert spent.reconnect_attempts == len(RETRY_DELAYS)
    async with transaction(f.db.first):
        assert await interrupted(owner)
        assert await reaped(owner)
    # Then the connection pauses with its credentials, and the reply is cancelled rather than left hanging.
    async with transaction(f.db.second):
        row = await get_personal(owner.agent_id)
        stored = await f.db.second.fetchval("SELECT count(*) FROM whatsapp_personal_auth")
        send = await f.db.second.fetchval(
            "SELECT state FROM whatsapp_personal_sends WHERE command_id=%(id)s", {"id": chunk.key}
        )
    assert row is not None
    assert row.state == "paused"
    assert not row.desired
    assert not row.purge_requested
    assert row.error_code == "transport_failed"
    assert row.account_id == owner.account_id
    assert stored == 1
    assert send == "cancelled"


async def test_pending_send_survives_a_transient_retry_and_dispatches_after_reconnect(channel_db):
    # Given a paired connection with one pending reply.
    f = channel_db
    async with transaction(f.db.first):
        owner = await paired(f)
        chunk = OutboundChunk(text="synthetic", key=uuid4())
        with pytest.raises(PersonalSendPendingError):
            await enqueue(target_for(owner), chunk)
    # When the child dies transiently and is reaped.
    async with transaction(f.db.first):
        assert await interrupted(owner)
        assert await reaped(owner)
    # Then the route still matches, so the relay keeps the reply pending instead of failing it.
    async with transaction(f.db.second):
        retrying = await get_personal(owner.agent_id)
        assert retrying is not None
        assert route_matches(retrying, PersonalRoute.from_external_id(target_for(owner).destination))
        with pytest.raises(PersonalSendPendingError):
            await enqueue(target_for(owner), chunk)
    # When the retry time passes and the bridge reconnects to the same account.
    async with transaction(f.db.first):
        await f.db.first.execute(
            "UPDATE channel_configs SET extra_config=jsonb_set(extra_config::jsonb,'{whatsapp_runtime,retry_at}',"
            "to_jsonb(now()-interval '1 second'))::json WHERE id=%(id)s",
            {"id": owner.config_id},
        )
        resumed = await claim(owner.config_id)
        assert resumed is not None
        assert owner.account_id is not None
        await handle_frame(
            resumed,
            Identity(
                type="identity",
                id="id",
                token="test",
                account_id=owner.account_id,
                pn_jid=owner.pn_jid,
                lid_jid=owner.lid_jid,
            ),
        )
        command = await authorize_dispatch(resumed)
    # Then the same reply is dispatched on the resumed claim.
    assert command is not None
    assert command.command_id == chunk.key


@pytest.mark.parametrize("relink", [False, True], ids=["start", "relink_while_claimed"])
async def test_owner_start_clears_the_retry_budget(channel_db, relink):
    # Given a paired connection waiting on a charged retry.
    f = channel_db
    async with transaction(f.db.first):
        owner = await paired(f)
        assert await interrupted(owner)
        if not relink:
            assert await reaped(owner)
    # When the owner starts it again or relinks it.
    async with transaction(f.db.second):
        await start(owner.agent_id, f.db.user.id, owner.generation, relink=relink)
        runtime = await runtime_for(owner.config_id)
        restarted = None if relink else await claim(owner.config_id)
    # Then the budget and retry gate are cleared, so a started connection is claimable at once.
    assert runtime.reconnect_attempts == 0
    assert runtime.retry_at is None
    assert relink or restarted is not None
