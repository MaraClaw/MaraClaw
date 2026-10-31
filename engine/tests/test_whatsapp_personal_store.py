from datetime import timedelta
from uuid import uuid4

import pytest
from cryptography.exceptions import InvalidTag
from fastapi import HTTPException
from tests.channel_submission_fixtures import channel_db, receipt_db

from app.dao.whatsapp_personal_auth_dao import AuthMutation, read_auth, write_auth
from app.dao.whatsapp_personal_dao import claim, get_personal, reaped, renew
from app.db.session import transaction
from app.services.channels.whatsapp_personal_control import disconnect, start
from app.services.channels.whatsapp_personal_crypto import AuthCipher, AuthKey

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
