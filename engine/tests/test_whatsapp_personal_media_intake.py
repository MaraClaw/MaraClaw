import json
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from tests.personal_burst_fixtures import LIMITS, scalar, submit, use_limits
from tests.whatsapp_personal_media_fixtures import channel_db, intake, linked, receipt_db

from app.dao.whatsapp_personal_dao import revoke
from app.db.session import transaction
from app.schemas.chat_wire import serialize_chat_server_event
from app.services.channels import whatsapp_personal_media as media
from app.services.channels.whatsapp_personal_intake import accept_message
from app.services.channels.whatsapp_personal_media_transport import MediaTransferError

__all__ = ["channel_db", "intake", "linked", "receipt_db"]


@pytest.mark.parametrize(
    ("kind", "filename", "caption"),
    [("file", "财务/报告.docx", "read report"), ("image", "photo.png", ""), ("video", "clip.mp4", "read report")],
)
async def test_media_refs_reach_original_transcript_live_event_and_burst_model_content_once(
    intake, kind, filename, caption
):
    original = intake.message()
    assert original.media is not None
    original = original.model_copy(
        update={"media": original.media.model_copy(update={"kind": kind, "filename": filename}), "text": caption}
    )
    download = AsyncMock(return_value=b"original bytes")
    assert await accept_message(intake.linked.owner, original, download=download)
    assert await accept_message(intake.linked.owner, original, download=download)
    download.assert_awaited_once()
    await intake.publish()
    async with transaction(intake.linked.f.db.second):
        row = await intake.linked.f.db.second.fetchone(
            "SELECT content, attachments FROM chat_messages WHERE role='user'"
        )
        assert row is not None
        refs = row["attachments"]
        assert isinstance(refs, list)
        assert len(refs) == 1
        ref = refs[0]
        assert set(ref) == {"path", "name"}
        assert "/" not in ref["name"]
        assert (intake.root / str(intake.agent_id) / ref["path"]).read_bytes() == b"original bytes"
        assert row["content"] == (f"{caption}\n" if caption else "") + f"[file: {ref['path']}]"
        assert (
            await intake.linked.f.db.second.fetchval("SELECT text FROM personal_burst_inputs WHERE outcome='accepted'")
            == row["content"]
        )
        assert await intake.linked.f.db.second.fetchval("SELECT count(*) FROM channel_event_receipts") == 1
    assert len(intake.events) == 1
    assert intake.audiences == [str(intake.linked.f.db.user.id)]
    event = serialize_chat_server_event(intake.events[0])
    assert event["attachments"] == refs
    assert "original bytes" not in json.dumps(event)
    assert (
        await scalar(
            intake.linked, "SELECT quota_messages_used FROM users WHERE id=%(id)s", {"id": intake.linked.f.db.user.id}
        )
        == 1
    )


@pytest.mark.parametrize("phase", ["before", "download", "publication"])
async def test_revoked_generation_never_leaves_a_published_upload_or_native_receipt(intake, monkeypatch, phase):
    async def revoke_source():
        async with transaction(intake.linked.f.db.first):
            await revoke(intake.agent_id, purge=True)

    async def download(_message, _limit):
        if phase == "download":
            await revoke_source()
        return b"original bytes"

    store = media.store_agent_upload

    async def publish(*args, **kwargs):
        result = await store(*args, **kwargs)
        await revoke_source()
        return result

    if phase == "before":
        await revoke_source()
    if phase == "publication":
        monkeypatch.setattr(media, "store_agent_upload", publish)
    read = AsyncMock(side_effect=download)
    with pytest.raises(HTTPException) as error:
        await accept_message(intake.linked.owner, intake.message(), download=read)
    assert error.value.status_code == 403
    if phase == "before":
        read.assert_not_awaited()
    assert not [path for path in intake.root.rglob("*") if path.is_file()]
    assert await scalar(intake.linked, "SELECT count(*) FROM channel_event_receipts") == 0
    assert await scalar(intake.linked, "SELECT count(*) FROM chat_messages WHERE role='user'") == 0


@pytest.mark.parametrize("declared", [False, True])
async def test_oversize_media_saves_a_deterministic_skip_original_and_replay_never_downloads(intake, declared):
    original = intake.message()
    assert original.media is not None
    original = original.model_copy(
        update={"media": original.media.model_copy(update={"size": 50 * 1024 * 1024 + 1 if declared else 0})}
    )
    download = AsyncMock(side_effect=MediaTransferError("too_large"))
    assert await accept_message(intake.linked.owner, original, download=download)
    assert await accept_message(intake.linked.owner, original, download=download)
    assert download.await_count == (0 if declared else 1)
    assert not [path for path in intake.root.rglob("*") if path.is_file()]
    await intake.publish()
    assert len(intake.events) == 1
    assert intake.events[0]["attachments"] == []
    assert intake.events[0]["content"] == "read report\n[file skipped: report.docx exceeds the 50 MiB file limit]"
    assert await scalar(intake.linked, "SELECT text FROM personal_burst_inputs") == intake.events[0]["content"]


@pytest.mark.parametrize("capacity", [False, True])
async def test_media_refusal_preserves_capacity_before_quota_and_removes_owned_upload(intake, monkeypatch, capacity):
    if capacity:
        use_limits(monkeypatch, replace(LIMITS, pending_count=1))
        await submit(intake.linked, "text-before-media")
    async with transaction(intake.linked.f.db.first):
        await intake.linked.f.db.first.execute("UPDATE users SET quota_message_limit=0")
    await accept_message(intake.linked.owner, intake.message(), download=AsyncMock(return_value=b"original bytes"))
    assert not [path for path in intake.root.rglob("*") if path.is_file()]
    assert await scalar(intake.linked, "SELECT outcome FROM personal_burst_inputs WHERE outcome <> 'accepted'") == (
        "capacity_rejected" if capacity else "quota_rejected"
    )
    assert await scalar(
        intake.linked, "SELECT quota_messages_used FROM users WHERE id=%(id)s", {"id": intake.linked.f.db.user.id}
    ) == int(capacity)


@pytest.mark.parametrize("code", ["expired", "download_failed", "forbidden", "invalid", "digest_mismatch"])
async def test_failed_media_transfer_leaves_native_event_undecided_and_connection_usable(intake, code):
    with pytest.raises(HTTPException) as error:
        await accept_message(
            intake.linked.owner, intake.message(), download=AsyncMock(side_effect=MediaTransferError(code))
        )
    assert error.value.status_code == 409
    assert await scalar(intake.linked, "SELECT count(*) FROM channel_event_receipts") == 0
    assert await scalar(intake.linked, "SELECT desired FROM whatsapp_personal_connections") is True
    assert not [path for path in intake.root.rglob("*") if path.is_file()]
