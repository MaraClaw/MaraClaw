import base64
from pathlib import Path

import anyio
import pytest
from pydantic import ValidationError
from tests.personal_burst_fixtures import scalar
from tests.whatsapp_personal_media_fixtures import channel_db, intake, linked, receipt_db

from app.services.channels import whatsapp_personal_child as module
from app.services.channels.whatsapp_personal_child import PersonalChild
from app.services.channels.whatsapp_personal_protocol import MediaChunk

__all__ = ["channel_db", "intake", "linked", "receipt_db"]
SCRIPT = Path(__file__).parents[1] / "connectors/whatsapp/dist/test/media-child.js"


@pytest.mark.parametrize("mode", ["normal", "burst", "wrapped"])
async def test_real_child_multi_chunk_acceptance_keeps_reader_available_and_replay_never_downloads(
    intake, monkeypatch, mode
):
    child = PersonalChild(intake.linked.owner)
    accepted = anyio.Event()
    handle = module.handle_message
    write = child.write
    receive = child.media.handle
    commands = []
    chunks = []
    seen = set()
    expected = 41 if mode == "burst" else 1

    async def forbidden(_message, _limit):
        raise AssertionError("replayed original must not request another download")

    async def accept(connection, message, download):
        await handle(connection, message, download)
        await handle(connection, message, forbidden)
        seen.add(message.message_id)
        if len(seen) == expected:
            accepted.set()

    async def observe_command(frame):
        if frame["type"] in {"media_get", "media_ack", "media_error"}:
            commands.append(frame)
        await write(frame)

    async def observe_chunk(frame):
        if isinstance(frame, MediaChunk):
            chunks.append((frame.seq, len(base64.b64decode(frame.data))))
        await receive(frame)

    monkeypatch.setattr(module, "handle_message", accept)
    monkeypatch.setattr(child, "write", observe_command)
    child.media.write = observe_command
    monkeypatch.setattr(child.media, "handle", observe_chunk)
    with anyio.fail_after(15):
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(child.run, ("node", str(SCRIPT), mode))
            await accepted.wait()
            tasks.cancel_scope.cancel()
    assert child.process is not None
    assert child.process.returncode == 0
    assert child.media.transfer is None
    assert chunks == [(0, 65536), (1, 65536), (2, 31)]
    assert [command["type"] for command in commands] == ["media_get", "media_ack", "media_ack", "media_ack"]
    assert (
        await scalar(intake.linked, "SELECT count(*) FROM personal_burst_inputs WHERE outcome='accepted'") == expected
    )
    assert await scalar(intake.linked, "SELECT count(*) FROM whatsapp_personal_auth") == 1
    assert await scalar(intake.linked, "SELECT desired FROM whatsapp_personal_connections") is True
    await intake.publish()
    assert len(intake.events) == expected
    refs = [event["attachments"] for event in intake.events if event["attachments"]]
    assert len(refs) == 1
    ref = refs[0][0]
    assert (intake.root / str(intake.agent_id) / ref["path"]).read_bytes() == b"c" * 131103


async def test_real_child_malformed_chunk_fails_closed_and_reaps_process_without_publication(intake):
    child = PersonalChild(intake.linked.owner)
    with anyio.fail_after(15), pytest.raises(ExceptionGroup) as raised:
        await child.run(("node", str(SCRIPT), "malformed"))
    matched, remainder = raised.value.split(ValidationError)
    assert matched is not None
    assert remainder is None
    assert child.process is not None
    assert child.process.returncode == 0
    assert child.media.transfer is None
    assert not [path for path in intake.root.rglob("*") if path.is_file()]
    assert await scalar(intake.linked, "SELECT count(*) FROM channel_event_receipts") == 0
    assert await scalar(intake.linked, "SELECT count(*) FROM chat_messages WHERE role='user'") == 0


async def test_real_child_declared_size_mismatch_keeps_credentials_and_accepts_the_following_text(intake, monkeypatch):
    child = PersonalChild(intake.linked.owner)
    accepted = anyio.Event()
    handle = module.handle_message

    async def accept(connection, message, download):
        await handle(connection, message, download)
        if message.message_id == "AFTER-MEDIA":
            accepted.set()

    monkeypatch.setattr(module, "handle_message", accept)
    with anyio.fail_after(15):
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(child.run, ("node", str(SCRIPT), "mismatch"))
            await accepted.wait()
            tasks.cancel_scope.cancel()
    assert child.process is not None
    assert child.process.returncode == 0
    assert child.media.transfer is None
    assert not [path for path in intake.root.rglob("*") if path.is_file()]
    assert await scalar(intake.linked, "SELECT count(*) FROM whatsapp_personal_auth") == 1
    assert await scalar(intake.linked, "SELECT desired FROM whatsapp_personal_connections") is True
    assert await scalar(intake.linked, "SELECT count(*) FROM channel_event_receipts") == 1
    assert (
        await scalar(intake.linked, "SELECT content FROM chat_messages WHERE role='user'") == "text after failed media"
    )


async def test_real_child_terminal_frame_drains_queued_text_before_revoking_its_source(intake):
    child = PersonalChild(intake.linked.owner)
    with anyio.fail_after(15):
        await child.run(("node", str(SCRIPT), "text-close"))
    assert child.process is not None
    assert child.process.returncode == 0
    assert await scalar(intake.linked, "SELECT count(*) FROM personal_burst_inputs WHERE outcome='accepted'") == 40
    assert await scalar(intake.linked, "SELECT count(*) FROM chat_messages WHERE role='user'") == 40
    assert await scalar(intake.linked, "SELECT count(*) FROM whatsapp_personal_auth") == 1
