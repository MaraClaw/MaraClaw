import hashlib
from dataclasses import replace
from unittest.mock import AsyncMock
from uuid import uuid4

import anyio
import pytest
from fastapi import HTTPException
from tests.zalo_media_fixtures import channel_db, media_db, media_message, receipt_db

from app.dao import channel_event_media_dao
from app.db.session import connection_ctx, get_connection, transaction
from app.records.channel_media import StoredFile
from app.services.channels import channel_media as media, submissions
from app.services.channels.zalo_personal_intake import intake
from app.services.organization_plans import plan_for, tier_for_tenant
from app.services.quota_guard import QuotaExceededError
from app.services.storage_runtime.quota_errors import StorageKeyPendingError, StorageQuotaExceededError

__all__ = ["channel_db", "media_db", "receipt_db"]


@pytest.mark.parametrize("media_db", ["local", "s3"], indirect=True)
@pytest.mark.parametrize("kind", ["image", "file"])
async def test_authorized_offer_commits_bytes_attachment_quota_and_native_intent_once(media_db, kind):
    world = media_db
    data = b"\x89PNG\r\n\x1a\nimage" if kind == "image" else b"%PDF-1.7\nDocument"
    calls = 0

    async def download(_frame):
        nonlocal calls
        assert get_connection() is None
        calls += 1
        return data

    await intake(world.connection, media_message(kind=kind), download=download)
    await intake(world.connection, media_message(kind=kind), download=download)
    async with connection_ctx() as db:
        counts = await db.fetchone(
            "SELECT (SELECT count(*) FROM channel_event_receipts) receipts, "
            "(SELECT count(*) FROM channel_event_media) media, (SELECT count(*) FROM chat_messages) transcript, "
            "(SELECT count(*) FROM gateway_messages) work, (SELECT count(*) FROM gateway_report_deliveries) intents, "
            "(SELECT quota_messages_used FROM users WHERE id = %(user)s) charged",
            {"user": world.channel.sender_id},
        )
        row = await db.fetchone("SELECT stored_file FROM channel_event_media")
        transcript = await db.fetchone("SELECT content, attachments FROM chat_messages")
    assert counts == {"receipts": 1, "media": 1, "transcript": 1, "work": 1, "intents": 1, "charged": 1}
    assert calls == 1
    assert row is not None
    assert transcript is not None
    file = StoredFile.model_validate(row["stored_file"])
    assert file.sha256 == hashlib.sha256(data).hexdigest()
    assert file.size == len(data)
    assert transcript == {"content": "", "attachments": [{"path": file.path, "name": file.name}]}
    assert (world.root / file.path).read_bytes() == data
    assert await world.storage.read_bytes_bounded(file.storage_key, len(data)) == data
    if world.server is not None:
        async with connection_ctx() as db:
            assert await db.fetchval(
                "SELECT charged_bytes FROM agent_storage_usage WHERE agent_id=%(id)s", {"id": world.agent.id}
            ) == len(data)


@pytest.mark.parametrize("media_db", ["local", "s3"], indirect=True)
async def test_concurrent_replay_cleans_only_losing_attempt(media_db):
    world = media_db
    entered, release = anyio.Event(), anyio.Event()
    downloads = 0

    async def download(_frame):
        nonlocal downloads
        assert get_connection() is None
        downloads += 1
        if downloads == 2:
            entered.set()
        await release.wait()
        return b"one durable offer"

    async def accept():
        await intake(world.connection, media_message(), download=download)

    with anyio.fail_after(15):
        async with anyio.create_task_group() as group:
            group.start_soon(accept)
            group.start_soon(accept)
            await entered.wait()
            release.set()
    async with connection_ctx() as db:
        row = await db.fetchone("SELECT stored_file FROM channel_event_media")
        assert await db.fetchval("SELECT count(*) FROM channel_event_receipts") == 1
        assert await db.fetchval("SELECT count(*) FROM gateway_messages") == 1
        assert await db.fetchval("SELECT count(*) FROM gateway_report_deliveries") == 1
        assert (
            await db.fetchval("SELECT quota_messages_used FROM users WHERE id=%(id)s", {"id": world.channel.sender_id})
            == 1
        )
    assert row is not None
    file = StoredFile.model_validate(row["stored_file"])
    assert (world.root / file.path).read_bytes() == b"one durable offer"
    assert list(world.root.rglob("offer.pdf")) == [world.root / file.path]
    if world.server is not None:
        assert list(world.server.state.objects) == [file.storage_key]
        async with connection_ctx() as db:
            assert (
                await db.fetchval(
                    "SELECT charged_bytes FROM agent_storage_usage WHERE agent_id=%(id)s", {"id": world.agent.id}
                )
                == file.size
            )


@pytest.mark.parametrize("refusal", ["unbound", "inactive", "stale_source", "group", "history"])
async def test_refusal_precedes_provider_bytes(media_db, refusal):
    world, frame = media_db, media_message()
    owned = world.connection
    async with connection_ctx() as db:
        if refusal == "unbound":
            await db.execute("DELETE FROM channel_sender_bindings")
        elif refusal == "inactive":
            await db.execute("UPDATE users SET is_active=FALSE WHERE id=%(id)s", {"id": world.channel.sender_id})
        elif refusal == "stale_source":
            owned = replace(owned, generation=world.agent.id)
        elif refusal == "group":
            frame = frame.model_copy(update={"thread_id": "111"})
        else:
            frame = frame.model_copy(update={"timestamp": 0})
    download = AsyncMock(return_value=b"refused")
    if refusal in ("inactive", "stale_source"):
        with pytest.raises(HTTPException) as raised:
            await intake(owned, frame, download=download)
        assert raised.value.status_code == {"inactive": 401, "stale_source": 409}[refusal]
    else:
        await intake(owned, frame, download=download)
    download.assert_not_awaited()
    async with connection_ctx() as db:
        assert await db.fetchval("SELECT count(*) FROM channel_event_receipts") == 0


@pytest.mark.parametrize("failure", ["revoked", "quota"])
async def test_reauthorization_and_quota_failure_roll_back_every_acceptance_write(media_db, failure):
    world = media_db

    async def download(_frame):
        async with connection_ctx() as db:
            await db.execute(
                "UPDATE users SET is_active=FALSE WHERE id=%(id)s"
                if failure == "revoked"
                else "UPDATE users SET quota_messages_used=quota_message_limit WHERE id=%(id)s",
                {"id": world.channel.sender_id},
            )
        return b"must not survive"

    with pytest.raises(HTTPException if failure == "revoked" else QuotaExceededError):
        await intake(world.connection, media_message(), download=download)
    async with connection_ctx() as db:
        assert (
            await db.fetchval(
                "SELECT (SELECT count(*) FROM channel_event_receipts) + (SELECT count(*) FROM channel_event_media) + "
                "(SELECT count(*) FROM chat_messages) + (SELECT count(*) FROM gateway_messages) + "
                "(SELECT count(*) FROM gateway_report_deliveries)"
            )
            == 0
        )
    assert list(world.root.rglob("offer.pdf")) == []


async def test_commit_acknowledgement_failure_never_deletes_durable_winner(media_db, monkeypatch):
    world = media_db
    accept = submissions.accept_channel_event

    async def ambiguous(event):
        await accept(event)
        raise ConnectionError("commit acknowledgement lost")

    monkeypatch.setattr(submissions, "accept_channel_event", ambiguous)
    with pytest.raises(ConnectionError):
        await intake(world.connection, media_message(), download=AsyncMock(return_value=b"committed"))
    async with transaction(world.channel.db.first):
        row = await world.channel.db.first.fetchone("SELECT stored_file FROM channel_event_media")
    assert row is not None
    file = StoredFile.model_validate(row["stored_file"])
    assert await channel_event_media_dao.references(file.storage_key)
    assert (world.root / file.path).read_bytes() == b"committed"


@pytest.mark.parametrize("media_db", ["s3"], indirect=True)
async def test_ambiguous_s3_create_is_explicit_and_keeps_charge_without_acceptance(media_db, monkeypatch):
    world = media_db
    attempt = uuid4()
    monkeypatch.setattr(media, "uuid4", lambda: attempt)
    key = f"{world.agent.id}/workspace/uploads/zalo/inbound/{attempt.hex}/offer.pdf"
    assert world.server is not None
    world.server.state.put_faults[key] = "reject_500"
    with pytest.raises(StorageKeyPendingError):
        await intake(world.connection, media_message(), download=AsyncMock(return_value=b"uncertain bytes"))
    async with connection_ctx() as db:
        pending = await db.fetchone("SELECT reserved_bytes, outcome_unknown FROM agent_storage_objects")
        assert pending == {"reserved_bytes": len(b"uncertain bytes"), "outcome_unknown": True}
        assert await db.fetchval("SELECT count(*) FROM channel_event_receipts") == 0
        assert await db.fetchval("SELECT count(*) FROM gateway_messages") == 0
        assert await db.fetchval("SELECT charged_bytes FROM agent_storage_usage") == len(b"uncertain bytes")
    assert world.server.state.count("DELETE", key) == 0


@pytest.mark.parametrize("media_db", ["s3"], indirect=True)
async def test_s3_storage_quota_refuses_before_provider_write(media_db):
    world = media_db
    async with connection_ctx() as db:
        tier = await tier_for_tenant(world.agent.tenant_id)
        assert tier is not None
        ceiling = int(plan_for(tier).storage_limit_bytes)
        await db.execute(
            "INSERT INTO agent_storage_usage (agent_id, charged_bytes) VALUES (%(id)s, %(used)s)",
            {"id": world.agent.id, "used": ceiling},
        )
    with pytest.raises(StorageQuotaExceededError):
        await intake(world.connection, media_message(), download=AsyncMock(return_value=b"full"))
    assert world.server is not None
    assert world.server.state.objects == {}
    async with connection_ctx() as db:
        assert await db.fetchval("SELECT charged_bytes FROM agent_storage_usage") == ceiling
        assert await db.fetchval("SELECT count(*) FROM channel_event_receipts") == 0


async def test_winning_descriptor_survives_gateway_history_removal(media_db):
    world = media_db
    await intake(world.connection, media_message(), download=AsyncMock(return_value=b"retained attachment"))
    async with connection_ctx() as db:
        row = await db.fetchone("SELECT stored_file FROM channel_event_media")
        assert row is not None
        file = StoredFile.model_validate(row["stored_file"])
        await db.execute("DELETE FROM gateway_messages")
    assert await channel_event_media_dao.references(file.storage_key)
    assert (world.root / file.path).read_bytes() == b"retained attachment"
