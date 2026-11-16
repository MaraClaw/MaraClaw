import base64
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
from cryptography.exceptions import InvalidTag
from fastapi import FastAPI, HTTPException
from tests.zalo_personal_fixtures import channel_db, claimed, connected, receipt_db

from app.api.zalo_personal import router
from app.core.security import get_current_user
from app.dao.zalo_personal_auth_dao import PURPOSE, read_auth, write_auth
from app.dao.zalo_personal_dao import claim, fenced, get_personal, reaped, renew, revoke
from app.db.errors import UniqueViolationError
from app.db.session import transaction
from app.services.channels.whatsapp_personal_crypto import AuthCipher, AuthKey
from app.services.channels.zalo_personal_control import disconnect, start, status
from app.services.channels.zalo_personal_events import handle_frame
from app.services.channels.zalo_personal_protocol import QR, Identity, Ready

__all__ = ["channel_db", "receipt_db"]


async def test_creator_api_is_no_store_and_fences_mutations(channel_db):
    f = channel_db
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_user] = lambda: f.db.user
    path = f"/agents/{f.db.agent.id}/zalo-channel/personal"
    async with (
        transaction(f.db.first),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://qa") as client,
    ):
        empty = await client.get(path)
        assert empty.status_code == 200
        assert empty.json()["generation"] is None
        started = await client.post(path + "/start", json={"expected_generation": None})
        assert started.status_code == 202
        generation = started.json()["generation"]
        assert started.headers["cache-control"] == "no-store, private"
        assert set(started.json()) == {
            "mode",
            "generation",
            "attempt_id",
            "revision",
            "state",
            "expires_at",
            "qr",
            "account_hint",
            "error_code",
            "resumable",
            "diagnostics",
        }
        assert (await client.post(path + "/start", json={"expected_generation": None})).status_code == 409
        assert (await client.post(path + "/relink", json={})).status_code == 422
        relinked = await client.post(path + "/relink", json={"expected_generation": generation})
        assert relinked.status_code == 202
        assert relinked.json()["generation"] != generation
        stopped = await client.delete(path, params={"expected_generation": relinked.json()["generation"]})
        assert stopped.status_code == 202
        assert stopped.json()["state"] == "disconnected"
        app.dependency_overrides[get_current_user] = lambda: replace(f.db.user, id=f.sender_id)
        assert (await client.get(path)).status_code == 403


async def test_encrypted_qr_credentials_identity_then_ready_and_restart(channel_db):
    f = channel_db
    png = base64.b64encode(b"\x89PNG\r\n\x1a\nimage").decode()
    async with transaction(f.db.first):
        owned = await claimed(f)
        await handle_frame(
            owned,
            QR(type="qr", id="qr", token="test", value=png, expires_at=datetime.now(UTC) + timedelta(seconds=100)),
        )
        public = await status(owned.agent_id, f.db.user.id)
        assert public.qr is not None
        assert public.qr.value == png
        raw_qr = await f.db.first.fetchval("SELECT qr_value FROM zalo_personal_connections")
        assert png.encode() not in raw_qr
        await write_auth(owned, "session-secret")
        sealed = await f.db.first.fetchval("SELECT ciphertext FROM zalo_personal_auth")
        assert b"session-secret" not in sealed
        with pytest.raises(InvalidTag):
            AuthCipher("wrong", purpose=PURPOSE).open(
                AuthKey(owned.config_id, owned.auth_epoch, "creds", "main"), sealed
            )
        await handle_frame(owned, Identity(type="identity", id="identity", token="test", account_id="123456789"))
        assert (await status(owned.agent_id, f.db.user.id)).state == "reconnecting"
        await handle_frame(owned, Ready(type="ready", id="ready", token="test"))
        assert (await status(owned.agent_id, f.db.user.id)).state == "connected"
        assert await reaped(owned)
        restored = await claim(owned.config_id)
        assert restored is not None
        assert await read_auth(restored) == "session-secret"
        assert restored.state == "reconnecting"
        await handle_frame(restored, Ready(type="ready", id="restored", token="test"))
        assert (await status(owned.agent_id, f.db.user.id)).state == "connected"


async def test_stale_or_expired_claim_cannot_work_or_release_account(channel_db):
    f = channel_db
    async with transaction(f.db.first):
        owned = await connected(f)
        with pytest.raises(HTTPException):
            await renew(replace(owned, claim_token=uuid4()))
        await f.db.first.execute("UPDATE zalo_personal_connections SET lease_until=now()-interval '1 second'")
        assert await claim(owned.config_id) is None
        with pytest.raises(HTTPException):
            await fenced(owned, connected=True)
        assert not await reaped(replace(owned, claim_token=uuid4()))
        await disconnect(owned.agent_id, f.db.user.id, owned.generation)
        row = await get_personal(owned.agent_id)
        assert row is not None
        assert row.account_id == owned.account_id
        assert row.state == "disconnecting"
        assert await reaped(owned)
        row = await get_personal(owned.agent_id)
        assert row is not None
        assert row.account_id is None


async def test_relink_while_live_reaps_exact_claim_then_starts_new_epoch(channel_db):
    f = channel_db
    async with transaction(f.db.first):
        owned = await connected(f)
        result = await start(owned.agent_id, f.db.user.id, owned.generation, relink=True)
        assert result.state == "disconnecting"
        assert result.generation != owned.generation
        with pytest.raises(HTTPException):
            await write_auth(owned, "late-credentials")
        assert await claim(owned.config_id) is None
        assert await reaped(owned)
        current = await claim(owned.config_id)
        assert current is not None
        assert current.auth_epoch != owned.auth_epoch
        assert await read_auth(current) is None


async def test_global_account_reservation_survives_lease_and_config_deletion(channel_db):
    f = channel_db
    second_agent = uuid4()
    async with transaction(f.db.first):
        first = await connected(f)
        await revoke(first.agent_id, purge=True)
        await f.db.first.execute("DELETE FROM channel_configs WHERE id=%(id)s", {"id": first.config_id})
        await f.db.first.execute(
            "INSERT INTO agents(id,name,creator_id,tenant_id,role_description,max_triggers,min_poll_interval_min,"
            "webhook_rate_limit,access_mode,company_access_level,heartbeat_interval_minutes) "
            "VALUES(%(id)s,'Other',%(owner)s,%(tenant)s,'',20,5,5,'company','use',240)",
            {"id": second_agent, "owner": f.db.user.id, "tenant": f.tenant_id},
        )
        await start(second_agent, f.db.user.id, None)
        row = await get_personal(second_agent)
        assert row is not None
        second = await claim(row.config_id)
        assert second is not None
        await write_auth(second, "second")
    with pytest.raises(UniqueViolationError):
        async with transaction(f.db.second):
            await handle_frame(second, Identity(type="identity", id="id", token="test", account_id="123456789"))
    async with transaction(f.db.first):
        assert await reaped(first)
        await handle_frame(second, Identity(type="identity", id="id", token="test", account_id="123456789"))
