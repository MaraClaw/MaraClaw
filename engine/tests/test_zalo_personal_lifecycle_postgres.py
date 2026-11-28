import sys
from datetime import timedelta
from pathlib import Path
from uuid import UUID, uuid4

import anyio
import pytest
from fastapi import HTTPException
from tests.channel_submission_fixtures import ChannelDatabase
from tests.personal_tick_fixtures import (
    TickStoppedError,
    between_ticks,
    planned_with_index,
    queries,
    record_frames,
    record_statements,
    touching,
)
from tests.test_whatsapp_personal_surfaces import use_pool
from tests.zalo_personal_fixtures import bind, channel_db, claimed, connected, message_for, receipt_db, route_for
from tests.zalo_personal_lifecycle_fixtures import BRIDGE

from app.dao.agent_dao import agent_dao
from app.dao.gateway_report_delivery_dao import gateway_report_delivery_dao
from app.dao.zalo_personal_auth_dao import read_auth
from app.dao.zalo_personal_dao import claim, get_personal, reaped
from app.db.pool import close_pool, init_pool
from app.db.session import transaction
from app.records.outbound_plan import OutboundChunk
from app.records.zalo_personal import ZaloConnection
from app.services import gateway_report_relay, gateway_report_wake, openclaw_routing, quota_guard
from app.services.agent_manager import agent_manager
from app.services.channels import zalo_personal_child as child_module
from app.services.channels.whatsapp_personal_journal import PersonalSendTerminalError
from app.services.channels.zalo_personal_access import zalo_owner
from app.services.channels.zalo_personal_child import ZaloChild
from app.services.channels.zalo_personal_control import disconnect, start, status
from app.services.channels.zalo_personal_events import handle_frame
from app.services.channels.zalo_personal_journal import enqueue
from app.services.channels.zalo_personal_protocol import ChildFrame, CredsSet
from app.services.openclaw_gateway_delivery import settle_native_report
from app.services.organization_plans import tier_for_tenant

__all__ = ["channel_db", "receipt_db"]


async def test_pair_to_reply_relink_and_disconnect_commits_across_real_child(
    channel_db: ChannelDatabase, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    f = channel_db
    # Reuse the fixture's model preparation and wake isolation, but retain the real organization quota policy.
    monkeypatch.setattr(quota_guard, "tier_for_tenant", tier_for_tenant)
    monkeypatch.setattr(agent_manager, "_agent_dir", lambda _id: tmp_path / str(_id))
    monkeypatch.setattr(gateway_report_wake, "schedule_background_task", lambda coro, _description: coro.close())
    signals = {name: anyio.Event() for name in ("qr", "ready", "unbound", "replay", "send_result")}
    gates = {name: anyio.Event() for name in ("qr", "unbound")}
    original = child_module.handle_frame

    async def observed(connection: ZaloConnection, frame: ChildFrame) -> None:
        await original(connection, frame)
        if frame.id in signals:
            signals[frame.id].set()
        if frame.id in gates:
            await gates[frame.id].wait()

    monkeypatch.setattr(child_module, "handle_frame", observed)
    async with transaction(f.db.first):
        assert (await status(f.db.agent.id, f.db.user.id)).generation is None
        owned = await claimed(f)
    await init_pool(conninfo=f.db.conninfo, min_size=1, max_size=4)
    old_destination: str | None = None
    try:
        child = ZaloChild(owned)
        with anyio.fail_after(20):
            async with anyio.create_task_group() as tasks:
                tasks.start_soon(child.run, (sys.executable, "-u", "-c", BRIDGE, f.db.conninfo, "messages"))
                await signals["qr"].wait()
                async with transaction(f.db.second):
                    pairing = await status(owned.agent_id, f.db.user.id)
                    assert pairing.state == "qr"
                    assert pairing.qr is not None
                    sealed_qr = await f.db.second.fetchval("SELECT qr_value FROM zalo_personal_connections")
                    assert isinstance(sealed_qr, bytes)
                    assert pairing.qr.value.encode() not in sealed_qr
                gates["qr"].set()
                await signals["unbound"].wait()
                async with transaction(f.db.second):
                    current = await get_personal(owned.agent_id)
                    assert current is not None
                    assert (current.state, current.account_id) == ("connected", "123456789")
                    assert current.activated_at is not None
                    old_destination = route_for(current).external_id()
                    assert await read_auth(current) == "opaque-session"
                    assert await f.db.second.fetchval(
                        "SELECT is_connected FROM channel_configs WHERE id=%(id)s", {"id": current.config_id}
                    )
                    assert await f.db.second.fetchval("SELECT count(*) FROM channel_event_receipts") == 0
                    assert await f.db.second.fetchval("SELECT count(*) FROM agent_daily_message_usage") == 0
                async with transaction(f.db.first):
                    await bind(f, current)
                gates["unbound"].set()
                await signals["replay"].wait()
                async with transaction(f.db.second):
                    receipt = await f.db.second.fetchone("SELECT event_key,conversation_id FROM channel_event_receipts")
                    assert receipt == {
                        "event_key": f"personal:{owned.auth_epoch}:incoming-1",
                        "conversation_id": route_for(current).external_id(),
                    }
                    assert await f.db.second.fetchval("SELECT count(*) FROM channel_event_receipts") == 1
                    canonical = await f.db.second.fetchval(
                        "SELECT id FROM chat_sessions WHERE agent_id=%(agent)s AND user_id=%(user)s "
                        "AND source_channel='web' AND is_primary AND NOT is_group",
                        {"agent": owned.agent_id, "user": f.sender_id},
                    )
                    assert canonical is not None
                    assert await f.db.second.fetchall(
                        "SELECT user_id,content,conversation_id FROM chat_messages WHERE role='user'"
                    ) == [{"user_id": f.sender_id, "content": "hello", "conversation_id": str(canonical)}]
                    assert await f.db.second.fetchval("SELECT messages_used FROM agent_daily_message_usage") == 1
                    assert (
                        await f.db.second.fetchval(
                            "SELECT quota_messages_used FROM users WHERE id=%(id)s", {"id": f.sender_id}
                        )
                        == 0
                    )
                    gateway = await f.db.second.fetchone("SELECT id,sender_user_id,status FROM gateway_messages")
                    assert gateway is not None
                    assert (gateway["sender_user_id"], gateway["status"]) == (f.sender_id, "pending")
                    assert await f.db.second.fetchval("SELECT count(*) FROM gateway_messages") == 1
                    intent = await f.db.second.fetchone("SELECT id,destination_id FROM gateway_report_deliveries")
                    assert intent is not None
                    assert (
                        await f.db.second.fetchval(
                            "SELECT external_conv_id FROM chat_sessions WHERE id=%(id)s",
                            {"id": intent["destination_id"]},
                        )
                        == route_for(current).external_id()
                    )
                agent = await agent_dao.get(owned.agent_id)
                assert agent is not None
                dispatched = await openclaw_routing._claim_wake(agent, UUID(str(gateway["id"])))
                assert dispatched is not None
                assert dispatched.id == gateway["id"]
                report = await settle_native_report(
                    agent, dispatched.id, "reply", binding_id=dispatched.reply_binding_id
                )
                assert report is not None
                assert await settle_native_report(agent, dispatched.id, "duplicate") is None
                assert await gateway_report_relay.relay_batch() == 1
                await signals["send_result"].wait()
                async with transaction(f.db.second):
                    delivery = await gateway_report_delivery_dao.get(UUID(str(intent["id"])))
                    assert delivery is not None
                    assert delivery.im_plan is not None
                    assert (delivery.next_chunk, delivery.delivered_at) == (0, None)
                    send = await f.db.second.fetchone(
                        "SELECT command_id,state,message_id,claim_token FROM zalo_personal_sends"
                    )
                    assert send == {
                        "command_id": delivery.im_plan.chunks[0].key,
                        "state": "accepted",
                        "message_id": "sdk-message",
                        "claim_token": owned.claim_token,
                    }
                    assert delivery.available_at is not None
                    retry_at = delivery.available_at + timedelta(seconds=1)
                assert await gateway_report_relay.relay_batch(lambda: retry_at) == 1
                async with transaction(f.db.second):
                    delivery = await gateway_report_delivery_dao.get(UUID(str(intent["id"])))
                    assert delivery is not None
                    assert delivery.delivered_at is not None
                    assert (delivery.next_chunk, delivery.terminal_at) == (1, None)
                    assert await f.db.second.fetchval("SELECT count(*) FROM zalo_personal_sends") == 1
                    assert await f.db.second.fetchall("SELECT content FROM chat_messages WHERE role='assistant'") == [
                        {"content": "reply"}
                    ]
                relink = await start(owned.agent_id, f.db.user.id, owned.generation, relink=True)
                assert relink.state == "disconnecting"
                assert relink.generation != owned.generation
                with pytest.raises(HTTPException, match="revoked"):
                    await handle_frame(owned, message_for().model_copy(update={"message_id": "late-old"}))
        assert child.process is not None
        assert child.process.returncode == 0
        async with transaction(f.db.second):
            pending = await get_personal(owned.agent_id)
            assert pending is not None
            assert (pending.state, pending.desired, pending.claim_token) == ("pending", True, None)
            assert pending.auth_epoch != owned.auth_epoch
            assert pending.account_id is None
            assert await f.db.second.fetchval("SELECT count(*) FROM zalo_personal_auth") == 0
            replacement_owner = await claim(owned.config_id)
            assert replacement_owner is not None
        assert old_destination is not None
        signals = {"ready": anyio.Event()}
        gates = {}
        replacement = ZaloChild(replacement_owner)
        with anyio.fail_after(20):
            async with anyio.create_task_group() as tasks:
                tasks.start_soon(replacement.run, (sys.executable, "-u", "-c", BRIDGE, f.db.conninfo, "pair"))
                await signals["ready"].wait()
                with pytest.raises(HTTPException, match="revoked"):
                    await handle_frame(owned, CredsSet(type="creds_set", id="late", token="test", creds="late-auth"))
                assert not await reaped(owned)
                with pytest.raises(PersonalSendTerminalError, match="zalo_route_revoked"):
                    await enqueue(old_destination, OutboundChunk(key=uuid4(), text="reply"))
                async with transaction(f.db.second):
                    active = await get_personal(owned.agent_id)
                    assert active is not None
                    assert active.state == "connected"
                    assert active.claim_token == replacement_owner.claim_token
                    assert active.generation == pending.generation
                    assert await read_auth(active) == "opaque-session"
                    assert await f.db.second.fetchval("SELECT count(*) FROM channel_event_receipts") == 1
                stopped = await disconnect(active.agent_id, f.db.user.id, active.generation)
                assert stopped.state == "disconnecting"
        assert replacement.process is not None
        assert replacement.process.returncode == 0
        async with transaction(f.db.second):
            final = await get_personal(owned.agent_id)
            assert final is not None
            assert (final.state, final.desired) == ("disconnected", False)
            assert (final.claim_token, final.lease_until, final.account_id) == (None, None, None)
            assert await f.db.second.fetchval("SELECT count(*) FROM zalo_personal_auth") == 0
            assert not await f.db.second.fetchval(
                "SELECT is_connected FROM channel_configs WHERE id=%(id)s", {"id": owned.config_id}
            )
    finally:
        await close_pool()


async def test_idle_tick_runs_one_owner_check_one_renewal_and_lock_free_probes(
    channel_db: ChannelDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given a connected account with nothing to send.
    f = channel_db
    use_pool(f, monkeypatch)
    async with transaction(f.db.first):
        owned = await connected(f)
    statements = record_statements(monkeypatch)
    async with transaction(f.db.first):
        await zalo_owner(owned.agent_id, authorization_only=True)
    owner_check = len(statements)
    statements.clear()
    frames = record_frames(monkeypatch, ZaloChild)
    between_ticks(monkeypatch)
    # When it ticks once.
    with anyio.fail_after(10), pytest.raises(TickStoppedError):
        await ZaloChild(owned).tick()
    # Then it spent one owner check, one connection read, one renewal and two probes, and took no row lock.
    assert len(statements) == owner_check + 4
    assert touching(statements, "zalo_personal_") == [
        ("SELECT", "zalo_personal_connections"),
        ("UPDATE", "zalo_personal_connections"),
        ("SELECT", "zalo_personal_sends"),
        ("SELECT", "zalo_personal_media_sends"),
    ]
    assert not queries(statements, "FOR UPDATE")
    # And each probe reads its pending partial index, and the lease frame is the only frame, with 30000 ms.
    for probe, index in zip(
        statements[-2:], ("ix_zalo_personal_pending", "ix_zalo_personal_media_sends_pending"), strict=True
    ):
        assert index in await planned_with_index(f.db.first, probe)
    assert [(frame["type"], frame["lease_ms"]) for frame in frames] == [("lease", 30000)]
