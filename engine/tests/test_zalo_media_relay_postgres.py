from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, assert_never
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from psycopg.pq import TransactionStatus
from tests.channel_submission_fixtures import ChannelDatabase
from tests.zalo_personal_fixtures import bind, channel_db, connected, receipt_db, route_for

from app.dao.gateway_report_delivery_dao import gateway_report_delivery_dao as dao
from app.db import session as db_session
from app.db.session import get_connection, transaction
from app.records.gateway_report_delivery import GatewayReportDeliveryRecord
from app.records.outbound_plan import OutboundFileChunk
from app.records.zalo_personal import ZaloConnection
from app.services import gateway_report_relay as relay
from app.services.channels import channel_media
from app.services.channels.outbound_plan import build_plan, configured_outbound
from app.services.channels.zalo_personal_child import ZaloChild
from app.services.channels.zalo_personal_events import handle_frame
from app.services.channels.zalo_personal_journal import authorize_dispatch
from app.services.channels.zalo_personal_media_jobs import send_pending_media
from app.services.channels.zalo_personal_media_journal import authorize_media_dispatch, pending_media, reject_media
from app.services.channels.zalo_personal_protocol import SendResult
from app.services.openclaw_transcript import ReplyMediaError
from app.services.storage_runtime.local import LocalStorageBackend

__all__ = ["channel_db", "receipt_db"]


@dataclass(frozen=True, slots=True)
class MediaRelay:
    fixture: ChannelDatabase
    owned: ZaloConnection
    intent: GatewayReportDeliveryRecord
    guest: Path
    storage: LocalStorageBackend


@pytest.fixture
async def media_relay(channel_db: ChannelDatabase, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> MediaRelay:
    f = channel_db

    class Pool:
        @asynccontextmanager
        async def connection(self):
            yield f.db.first.raw

    monkeypatch.setattr(db_session, "get_pool", lambda: Pool())
    guest = tmp_path / "guest"
    (guest / "workspace").mkdir(parents=True)
    (guest / "workspace/report.pdf").write_bytes(b"private document")
    storage = LocalStorageBackend(str(tmp_path / "storage"))
    monkeypatch.setattr(channel_media.agent_manager, "_agent_dir", lambda _: guest)
    monkeypatch.setattr(channel_media, "get_storage_backend", lambda: storage)
    original = channel_media.snapshot_reply_media

    async def checked_snapshot(agent, result):
        assert get_connection() is None
        assert f.db.first.raw.info.transaction_status == TransactionStatus.IDLE
        return await original(agent, result)

    monkeypatch.setattr(channel_media, "snapshot_reply_media", checked_snapshot)
    async with transaction(f.db.first):
        owned = await connected(f)
        await bind(f, owned)
        await f.db.first.execute(
            "UPDATE chat_sessions SET source_channel='zalo',external_conv_id=%(route)s WHERE id=%(id)s",
            {"route": route_for(owned).external_id(), "id": f.db.session_id},
        )
        message_id = uuid4()
        await f.db.first.execute(
            "INSERT INTO gateway_messages(id,agent_id,content,status,result) "
            "VALUES(%(id)s,%(agent)s,'request','completed',%(result)s)",
            {"id": message_id, "agent": f.db.agent.id, "result": "MEDIA:/home/node/custom-state/workspace/report.pdf"},
        )
        intent = await dao.create_intent(
            message_id=message_id, destination_kind="im", destination_id=f.db.session_id, tenant_id=f.tenant_id
        )
    assert intent is not None
    return MediaRelay(f, owned, intent, guest, storage)


async def due(context: MediaRelay) -> None:
    async with transaction(context.fixture.db.second):
        await context.fixture.db.second.execute(
            "UPDATE gateway_report_deliveries SET available_at=clock_timestamp() WHERE id=%(id)s",
            {"id": context.intent.id},
        )


async def test_file_only_relay_snapshots_once_and_checkpoints_attachment_ack(media_relay: MediaRelay) -> None:
    context, f = media_relay, media_relay.fixture
    assert await relay.relay_batch() == 1
    queued = await pending_media(context.owned)
    assert queued is not None
    assert queued.file.name == "report.pdf"
    first = await dao.get(context.intent.id)
    assert first is not None
    assert first.im_plan is not None
    assert first.im_plan.version == 2
    assert first.im_plan.chunks == (OutboundFileChunk(key=queued.command_id, file=queued.file),)
    assert first.next_chunk == 0
    assert first.delivered_at is None
    async with transaction(f.db.first):
        assert await f.db.first.fetchval("SELECT count(*) FROM zalo_personal_sends") == 0
    (context.guest / "workspace/report.pdf").write_bytes(b"changed original after snapshot")
    async with transaction(f.db.second):
        await f.db.second.execute("UPDATE gateway_messages SET result='changed after restart'")
    await due(context)
    assert await relay.relay_batch() == 1
    assert await pending_media(context.owned) == queued
    assert get_connection() is None
    assert f.db.first.raw.info.transaction_status == TransactionStatus.IDLE
    staged = await channel_media.read_stored_media(f.db.agent, queued.file)
    assert staged == b"private document"
    command = await authorize_media_dispatch(context.owned, queued.command_id)
    assert command is not None
    await handle_frame(
        context.owned,
        SendResult(
            type="send_result",
            id="attachment-ack",
            token="test",
            command_id=str(command.command_id),
            outcome="accepted",
            message_id="attachment-native-1",
        ),
    )
    await due(context)
    assert await relay.relay_batch() == 1
    delivered = await dao.get(context.intent.id)
    assert delivered is not None
    assert delivered.delivered_at is not None
    assert delivered.next_chunk == 1
    assert delivered.im_plan == first.im_plan
    assert await pending_media(context.owned) is None
    assert await authorize_media_dispatch(context.owned, command.command_id) is None
    assert await relay.relay_batch() == 0


@pytest.mark.parametrize("damage", ["changed", "grown", "missing"])
async def test_damaged_snapshot_rejects_only_its_command_without_stopping_connection(
    media_relay: MediaRelay, monkeypatch: pytest.MonkeyPatch, damage: Literal["changed", "grown", "missing"]
) -> None:
    context, f = media_relay, media_relay.fixture
    assert await relay.relay_batch() == 1
    queued = await pending_media(context.owned)
    assert queued is not None
    match damage:
        case "changed":
            await context.storage.write_bytes(queued.file.storage_key, b"x" * queued.file.size)
        case "grown":
            await context.storage.write_bytes(queued.file.storage_key, b"x" * (queued.file.size + 1))
        case "missing":
            await context.storage.delete(queued.file.storage_key)
        case unreachable:
            assert_never(unreachable)
    child = ZaloChild(context.owned)
    child.media_busy = True
    send = AsyncMock()
    monkeypatch.setattr(child, "send_file", send)
    await send_pending_media(child)
    assert child.media_busy is False
    send.assert_not_awaited()
    async with transaction(f.db.first):
        assert await f.db.first.fetchone(
            "SELECT state,dispatched_at FROM zalo_personal_media_sends WHERE command_id=%(id)s",
            {"id": queued.command_id},
        ) == {"state": "rejected", "dispatched_at": None}
        assert await f.db.first.fetchone("SELECT state,desired FROM zalo_personal_connections") == {
            "state": "connected",
            "desired": True,
        }
    await due(context)
    assert await relay.relay_batch() == 1
    terminal = await dao.get(context.intent.id)
    assert terminal is not None
    assert terminal.terminal_reason == "payload_rejected"


@pytest.mark.parametrize("damage", ["changed", "missing", "foreign"])
async def test_invalid_private_snapshot_is_rejected_before_dispatch(media_relay: MediaRelay, damage: str) -> None:
    context, f = media_relay, media_relay.fixture
    assert await relay.relay_batch() == 1
    queued = await pending_media(context.owned)
    assert queued is not None
    file = queued.file
    match damage:
        case "changed":
            await context.storage.write_bytes(file.storage_key, b"mutated content")
        case "missing":
            await context.storage.delete(file.storage_key)
        case "foreign":
            file = file.model_copy(update={"storage_key": f"{uuid4()}/{file.path}"})
    with pytest.raises((channel_media.ChannelMediaError, FileNotFoundError)):
        await channel_media.read_stored_media(f.db.agent, file)
    await reject_media(context.owned, queued.command_id)
    assert await authorize_media_dispatch(context.owned, queued.command_id) is None
    await due(context)
    assert await relay.relay_batch() == 1
    terminal = await dao.get(context.intent.id)
    assert terminal is not None
    assert terminal.terminal_reason == "payload_rejected"
    async with transaction(f.db.first):
        assert await f.db.first.fetchval("SELECT dispatched_at FROM zalo_personal_media_sends") is None


@pytest.mark.parametrize("result", ["MEDIA:/etc/passwd", "MEDIA:/home/node/custom-state/workspace/missing.pdf"])
async def test_missing_or_uncontained_media_result_is_terminal(media_relay: MediaRelay, result: str) -> None:
    context, f = media_relay, media_relay.fixture
    async with transaction(f.db.second):
        await f.db.second.execute("UPDATE gateway_messages SET result=%(result)s", {"result": result})
    assert await relay.relay_batch() == 1
    terminal = await dao.get(context.intent.id)
    assert terminal is not None
    assert terminal.terminal_reason == "payload_rejected"
    assert terminal.im_plan is None
    async with transaction(f.db.first):
        assert await f.db.first.fetchval("SELECT count(*) FROM zalo_personal_media_sends") == 0


async def test_text_is_accepted_before_one_file_command_is_enqueued(media_relay: MediaRelay) -> None:
    context, f = media_relay, media_relay.fixture
    async with transaction(f.db.second):
        await f.db.second.execute("UPDATE gateway_messages SET result='caption' || chr(10) || result")
    assert await relay.relay_batch() == 1
    assert await pending_media(context.owned) is None
    text = await authorize_dispatch(context.owned)
    assert text is not None
    assert text.text == "caption"
    await handle_frame(
        context.owned,
        SendResult(
            type="send_result",
            id="text-ack",
            token="test",
            command_id=str(text.command_id),
            outcome="accepted",
            message_id="text-native-1",
        ),
    )
    await due(context)
    assert await relay.relay_batch() == 1
    assert await pending_media(context.owned) is None
    assert await relay.relay_batch() == 1
    queued = await pending_media(context.owned)
    assert queued is not None
    assert queued.peer_id == "987654321"
    async with transaction(f.db.first):
        assert await f.db.first.fetchval("SELECT count(*) FROM zalo_personal_media_sends") == 1


@pytest.mark.parametrize("invalid_loser", [False, True])
async def test_concurrent_preparation_uses_the_winning_plan(
    media_relay: MediaRelay, monkeypatch: pytest.MonkeyPatch, invalid_loser: bool
) -> None:
    context, f = media_relay, media_relay.fixture
    original = channel_media.snapshot_reply_media
    _, agent = await relay._source(context.intent)
    session = await relay.chat_session_dao.get(f.db.session_id)
    assert session is not None
    target = (await configured_outbound(agent, session)).target

    async def racing_snapshot(agent, result):
        _, files = await original(agent, result)
        async with transaction(f.db.second):
            current = await dao.get(context.intent.id)
            assert current is not None
            assert await dao.initialize_plan(
                current, build_plan(target, "winning caption", current.id, files=files), now=datetime.now(UTC)
            )
        if invalid_loser:
            raise ReplyMediaError("missing_media_file")
        return "losing caption", files

    monkeypatch.setattr(channel_media, "snapshot_reply_media", racing_snapshot)
    assert await relay.relay_batch() == 1
    winning = await authorize_dispatch(context.owned)
    assert winning is not None
    assert winning.text == "winning caption"
    assert await pending_media(context.owned) is None


async def test_uncertain_attachment_finishes_relay_without_resend(media_relay: MediaRelay) -> None:
    context = media_relay
    assert await relay.relay_batch() == 1
    queued = await pending_media(context.owned)
    assert queued is not None
    assert await authorize_media_dispatch(context.owned, queued.command_id) is not None
    await handle_frame(
        context.owned,
        SendResult(
            type="send_result",
            id="uncertain",
            token="test",
            command_id=str(queued.command_id),
            outcome="uncertain",
            message_id="",
        ),
    )
    await due(context)
    assert await relay.relay_batch() == 1
    terminal = await dao.get(context.intent.id)
    assert terminal is not None
    assert terminal.terminal_reason == "payload_rejected"
    assert await pending_media(context.owned) is None
    assert await authorize_media_dispatch(context.owned, queued.command_id) is None
    assert await relay.relay_batch() == 0
