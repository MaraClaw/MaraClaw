from contextlib import closing
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from tests.test_openclaw_delayed_replies import append, transcript
from tests.test_openclaw_gateway_rpc import _Turn, wire_terminal
from tests.zalo_media_fixtures import channel_db, media_db, media_message, receipt_db

from app.api.websocket import manager
from app.dao import gateway_message_dao
from app.dao.channel_event_media_dao import channel_event_media_dao
from app.db.session import connection_ctx
from app.records.channel_media import StoredFile
from app.services import openclaw_gateway_rpc as rpc, openclaw_routing
from app.services.channels.channel_media import snapshot_reply_media
from app.services.channels.zalo_personal_intake import intake
from app.services.openclaw_gateway_delivery import settle_native_report
from app.services.openclaw_native_capacity import shutdown_native_capacity
from app.services.openclaw_turns import runtime_session_key

__all__ = ["channel_db", "media_db", "receipt_db"]


@pytest.mark.parametrize("media_db", ["local", "s3"], indirect=True)
async def test_native_rpc_rematerializes_accepted_bytes_and_uses_real_guest_paths(media_db, monkeypatch):
    world = media_db
    await intake(world.connection, media_message(), download=AsyncMock(return_value=b"mounted input"))
    async with connection_ctx() as db:
        row = await db.fetchone("SELECT * FROM channel_event_media")
        assert row is not None
        file = StoredFile.model_validate(row["stored_file"])
        message = await gateway_message_dao.get_for_agent(UUID(str(row["gateway_message_id"])), world.agent.id)
    assert message is not None
    if world.server is not None:
        (world.root / file.path).unlink()
    media_lookup = channel_event_media_dao.for_message
    zalo_lookup = channel_event_media_dao.is_zalo_turn
    turn = _Turn(monkeypatch, world.root)
    monkeypatch.setattr(channel_event_media_dao, "for_message", media_lookup)
    monkeypatch.setattr(channel_event_media_dao, "is_zalo_turn", zalo_lookup)
    turn.agent = world.agent
    turn.message = message
    monkeypatch.setattr(rpc, "streams_to_web", AsyncMock(return_value=False))
    monkeypatch.setattr(rpc, "clear_stopped_guest_loss", AsyncMock())
    payloads = []

    def execute(_container, payload_name, _cancel):
        import json

        payload = json.loads((world.root / payload_name).read_text())
        guest_path = f"/home/node/custom-state/{file.path}"
        assert guest_path in payload["message"]
        assert (world.root / file.path).read_bytes() == b"mounted input"
        assert "MEDIA:/home/node/custom-state/workspace/" in payload["extraSystemPrompt"]
        assert set(payload) == {
            "version",
            "messageId",
            "sessionKey",
            "message",
            "modelRef",
            "extraSystemPrompt",
            "timeoutSeconds",
        }
        payloads.append(payload)
        return wire_terminal(str(message.id))

    monkeypatch.setattr(rpc, "_execute", execute)
    try:
        outcome = await rpc.execute_gateway_turn(world.agent, message)
    finally:
        shutdown_native_capacity()
    assert isinstance(outcome, rpc.TerminalOutcome)
    assert len(payloads) == 1
    assert list(world.root.glob(".maraclaw-rpc-*")) == []


@pytest.mark.parametrize("source", ["marker", "flattened", "wrong_run", "wrong_session"])
@pytest.mark.parametrize("media_db", ["local", "s3"], indirect=True)
async def test_normal_terminal_file_only_completion_is_durable_and_visible(media_db, monkeypatch, source):
    world = media_db
    await intake(world.connection, media_message(), download=AsyncMock(return_value=b"input"))
    async with connection_ctx() as db:
        row = await db.fetchone("SELECT gateway_message_id FROM channel_event_media")
        assert row is not None
        await db.execute("UPDATE gateway_messages SET status='delivered', delivered_at=now()")
        message = await gateway_message_dao.get_for_agent(UUID(str(row["gateway_message_id"])), world.agent.id)
    assert message is not None
    output = world.root / "workspace/result.pdf"
    output.write_bytes(b"durable generated file")
    marker = "MEDIA:/home/node/custom-state/workspace/result.pdf"
    with closing(transcript(world.root / "agents/main/agent/openclaw-agent.sqlite")) as sqlite:
        sqlite.execute(
            "INSERT INTO session_windows VALUES (?, ?)",
            ("physical", runtime_session_key(message) if source != "wrong_session" else "different"),
        )
        append(sqlite, "physical", 1, run=str(message.id) if source != "wrong_run" else str(uuid4()), text=marker)
        monkeypatch.setattr(openclaw_routing, "_claim_wake", AsyncMock(return_value=message))
        monkeypatch.setattr(
            rpc,
            "execute_gateway_turn",
            AsyncMock(
                return_value=rpc.TerminalOutcome(
                    str(message.id), str(message.id), "ok", marker if source == "marker" else "", None
                )
            ),
        )
        send = AsyncMock()
        monkeypatch.setattr(manager, "send_to_session", send)
        await openclaw_routing._run_inbox_wake(world.agent, "", message.id)
    async with connection_ctx() as db:
        completed = await gateway_message_dao.get_for_agent(message.id, world.agent.id)
        assistant = await db.fetchone("SELECT content, attachments FROM chat_messages WHERE role='assistant'")
    assert completed is not None
    assert completed.status == "completed"
    assert assistant is not None
    if source in ("wrong_run", "wrong_session"):
        assert completed.result == "The agent completed the request without a text response."
        assert not assistant["attachments"]
    else:
        assert completed.result == marker
        assert assistant == {"content": "", "attachments": [{"path": "workspace/result.pdf", "name": "result.pdf"}]}
        assert send.await_args is not None
        event = send.await_args.args[2]
        assert event["content"] == ""
        assert event["attachments"] == assistant["attachments"]
        cleaned, files = await snapshot_reply_media(world.agent, completed.result)
        assert cleaned == ""
        assert len(files) == 1
        assert await world.storage.read_bytes_bounded(files[0].storage_key, files[0].size) == output.read_bytes()


@pytest.mark.parametrize("source", ["missing", "symlink", "foreign"])
async def test_invalid_output_file_has_explicit_durable_failure_without_attachment(media_db, tmp_path, source):
    world = media_db
    await intake(world.connection, media_message(), download=AsyncMock(return_value=b"input"))
    async with connection_ctx() as db:
        row = await db.fetchone("SELECT gateway_message_id FROM channel_event_media")
        assert row is not None
        await db.execute("UPDATE gateway_messages SET status='delivered', delivered_at=now()")
    marker = "MEDIA:/home/node/custom-state/workspace/result.pdf"
    if source == "symlink":
        secret = tmp_path / "secret.pdf"
        secret.write_bytes(b"foreign")
        (world.root / "workspace/result.pdf").symlink_to(secret)
    elif source == "foreign":
        marker = "MEDIA:/home/node/custom-state/enterprise_info/secret.pdf"
    report = await settle_native_report(world.agent, UUID(str(row["gateway_message_id"])), marker)
    assert report is not None
    assert report.message.result is not None
    assert "could not return the generated file" in report.message.result
    async with connection_ctx() as db:
        assistant = await db.fetchone("SELECT content, attachments FROM chat_messages WHERE role='assistant'")
    assert assistant is not None
    assert not assistant["attachments"]
