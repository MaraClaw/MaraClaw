from datetime import UTC, datetime
from uuid import uuid4

import anyio
import pytest
from fastapi import HTTPException
from tests.zalo_personal_fixtures import bind, channel_db, connected, receipt_db, route_for

from app.dao.zalo_personal_auth_dao import read_auth
from app.dao.zalo_personal_dao import candidates, claim, get_personal, reaped, renew, revoke
from app.dao.zalo_personal_runtime_dao import interrupted, runtime_for
from app.db.session import transaction
from app.records.outbound_plan import OutboundChunk
from app.services.channels.whatsapp_personal_journal import PersonalSendPendingError
from app.services.channels.zalo_personal_control import start, status
from app.services.channels.zalo_personal_events import handle_frame
from app.services.channels.zalo_personal_journal import authorize_dispatch, enqueue
from app.services.channels.zalo_personal_protocol import Closed, Heartbeat, Ready

__all__ = ["channel_db", "receipt_db"]


async def test_transport_close_preserves_encrypted_login_and_routing_fences(channel_db):
    f = channel_db
    async with transaction(f.db.first):
        owned = await connected(f)
        await bind(f, owned)
        with pytest.raises(PersonalSendPendingError):
            await enqueue(route_for(owned).external_id(), OutboundChunk(text="pending", key=uuid4()))
        dispatched = await authorize_dispatch(owned)
        assert dispatched is not None
        with pytest.raises(PersonalSendPendingError):
            await enqueue(route_for(owned).external_id(), OutboundChunk(text="still pending", key=uuid4()))
        sealed = await f.db.first.fetchval("SELECT ciphertext FROM zalo_personal_auth")
        await handle_frame(owned, Closed(type="closed", id="drop", token="test", reason="transport_failed"))
        current = await get_personal(owned.agent_id)
        assert current is not None
        assert current.desired
        assert current.state == "reconnecting"
        assert current.claim_token == owned.claim_token
        assert current.auth_epoch == owned.auth_epoch
        assert current.generation == owned.generation
        assert await f.db.first.fetchval("SELECT ciphertext FROM zalo_personal_auth") == sealed
        assert await reaped(owned)
        current = await get_personal(owned.agent_id)
        assert current is not None
        assert current.claim_token is None
        assert current.account_id == owned.account_id
        assert (
            await f.db.first.fetchval(
                "SELECT state FROM zalo_personal_sends WHERE command_id=%(id)s", {"id": dispatched.command_id}
            )
            == "uncertain"
        )
        assert await f.db.first.fetchval("SELECT count(*) FROM zalo_personal_sends WHERE state='pending'") == 1
        assert await claim(owned.config_id) is None
        assert await candidates() == []


async def test_backoff_is_durable_and_claim_race_has_one_winner(channel_db):
    f = channel_db
    async with transaction(f.db.first):
        owned = await connected(f)
        assert await interrupted(owned)
        assert not await interrupted(owned)
        assert (await runtime_for(owned.config_id)).reconnect_attempts == 1
        await reaped(owned)
        assert await candidates() == []
        assert await claim(owned.config_id) is None
        await f.db.first.execute(
            "UPDATE channel_configs SET extra_config=(extra_config::jsonb || "
            "jsonb_build_object('zalo_runtime',extra_config::jsonb->'zalo_runtime' || "
            "jsonb_build_object('retry_at',now()-interval '1 second')))::json"
        )
        assert len(await candidates()) == 1
    winners = []

    async def acquire(db):
        async with transaction(db):
            winners.append(await claim(owned.config_id))

    with anyio.fail_after(10):
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(acquire, f.db.first)
            tasks.start_soon(acquire, f.db.second)
    winner = next(row for row in winners if row is not None)
    assert sum(row is not None for row in winners) == 1
    assert winner.state == "reconnecting"
    assert winner.expires_at is not None
    assert 110 < (winner.expires_at - datetime.now(UTC)).total_seconds() <= 120


async def test_retry_exhaustion_resume_and_ready_flaps_preserve_budget(channel_db):
    f = channel_db
    async with transaction(f.db.first):
        owned = await connected(f)
        sealed = await f.db.first.fetchval("SELECT ciphertext FROM zalo_personal_auth")
        current = owned
        for attempt, delay in enumerate((5, 15, 30, 60, 120), start=1):
            before = datetime.now(UTC)
            assert await interrupted(current)
            runtime = await runtime_for(owned.config_id)
            assert runtime.reconnect_attempts == attempt
            assert runtime.retry_at is not None
            assert delay <= (runtime.retry_at - before).total_seconds() < delay + 2
            assert not await interrupted(current)
            await reaped(current)
            await f.db.first.execute(
                "UPDATE channel_configs SET extra_config=jsonb_set(extra_config::jsonb,"
                "'{zalo_runtime,retry_at}',to_jsonb(now()-interval '1 second'))::json"
            )
            restored = await claim(owned.config_id)
            assert restored is not None
            current = restored
            await handle_frame(current, Ready(type="ready", id="ready", token="test"))
            assert (await runtime_for(owned.config_id)).reconnect_attempts == attempt
        assert await interrupted(current)
        await reaped(current)
        paused = await status(owned.agent_id, f.db.user.id)
        assert paused.state == "failed"
        assert paused.resumable
        assert paused.diagnostics is not None
        assert paused.diagnostics.reconnect_attempts == 5
        assert await candidates() == []
        resumed = await start(owned.agent_id, f.db.user.id, paused.generation)
        assert resumed.generation == owned.generation
        assert resumed.state == "reconnecting"
        assert resumed.qr is None
        assert resumed.diagnostics is not None
        assert resumed.diagnostics.reconnect_attempts == 0
        current = await claim(owned.config_id)
        assert current is not None
        assert current.auth_epoch == owned.auth_epoch
        assert await read_auth(current) == "opaque-session"
        assert await f.db.first.fetchval("SELECT ciphertext FROM zalo_personal_auth") == sealed
        await handle_frame(current, Ready(type="ready", id="ready", token="test"))
        await handle_frame(current, Heartbeat(type="heartbeat", id="ping", token="test", ok=True))
        await interrupted(current)
        assert (await runtime_for(owned.config_id)).reconnect_attempts == 1


async def test_session_replacement_pauses_and_relink_explicitly_purges(channel_db):
    f = channel_db
    async with transaction(f.db.first):
        owned = await connected(f)
        await handle_frame(owned, Closed(type="closed", id="closed", token="test", reason="session_replaced"))
        await reaped(owned)
        paused = await status(owned.agent_id, f.db.user.id)
        assert paused.state == "paused"
        assert paused.resumable
        assert paused.diagnostics is not None
        assert paused.diagnostics.last_error == "session_replaced"
        assert paused.diagnostics.retry_at is None
        assert await candidates() == []
        await start(owned.agent_id, f.db.user.id, paused.generation, relink=True)
        row = await get_personal(owned.agent_id)
        assert row is not None
        assert row.auth_epoch != owned.auth_epoch
        assert await f.db.first.fetchval("SELECT count(*) FROM zalo_personal_auth") == 0


@pytest.mark.parametrize("change", ["revoke", "owner", "tenant"])
async def test_stale_sources_cannot_renew_or_resurrect_retry(channel_db, change):
    f = channel_db
    async with transaction(f.db.first):
        owned = await connected(f)
        if change == "revoke":
            await revoke(owned.agent_id, purge=False)
        elif change == "owner":
            await f.db.first.execute("UPDATE agents SET creator_id=%(id)s", {"id": f.sender_id})
        else:
            await f.db.first.execute("UPDATE agents SET tenant_id=NULL")
        current = await get_personal(owned.agent_id)
        assert current is not None
        with pytest.raises(HTTPException):
            await renew(owned)
        assert not await interrupted(owned)
        assert await get_personal(owned.agent_id) == current


async def test_restore_expiry_recovers_and_status_does_not_expire_backoff(channel_db):
    f = channel_db
    async with transaction(f.db.first):
        owned = await connected(f)
        await reaped(owned)
        current = await claim(owned.config_id)
        assert current is not None
        await f.db.first.execute("UPDATE zalo_personal_connections SET expires_at=now()-interval '1 second'")
        public = await status(owned.agent_id, f.db.user.id)
        assert public.state == "reconnecting"
        assert public.error_code == "transport_failed"
        await reaped(current)
        await f.db.first.execute("UPDATE zalo_personal_connections SET expires_at=now()-interval '1 second'")
        assert (await status(owned.agent_id, f.db.user.id)).state == "reconnecting"
        assert await f.db.first.fetchval("SELECT count(*) FROM zalo_personal_auth") == 1
