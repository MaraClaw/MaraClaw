"""Verified channel bytes, immutable reply snapshots and real mounted guest files."""

import hashlib
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Literal, assert_never, override
from uuid import uuid4

import anyio

from app.records.agent import AgentRecord
from app.records.channel_media import StoredFile
from app.records.chat_attachment import ChatAttachment
from app.services.agent_manager import agent_manager
from app.services.openclaw_transcript import recover_media
from app.services.openclaw_update_exclusion import guest_write_section
from app.services.storage_runtime.agent_files import agent_storage_key
from app.services.storage_runtime.base import StorageReadLimitExceededError
from app.services.storage_runtime.channel_files import publish_guest_file, read_guest_file
from app.services.storage_runtime.facade import get_storage_backend, guess_content_type

MAX_FILE_BYTES = 50 * 1024 * 1024
MAX_IMAGE_BYTES = 10 * 1024 * 1024


@dataclass(slots=True)
class ChannelMediaError(Exception):
    code: str

    @override
    def __str__(self) -> str:
        return self.code


def _check_bytes(data: bytes, kind: Literal["image", "file"]) -> None:
    match kind:
        case "image":
            if len(data) > MAX_IMAGE_BYTES:
                raise ChannelMediaError("image_too_large")
            if not (
                data.startswith((b"\xff\xd8\xff", b"\x89PNG\r\n\x1a\n"))
                or (data.startswith(b"RIFF") and data[8:12] == b"WEBP")
            ):
                raise ChannelMediaError("unsupported_image")
        case "file":
            if len(data) > MAX_FILE_BYTES:
                raise ChannelMediaError("file_too_large")
        case unreachable:
            assert_never(unreachable)


def _check_owner(agent: AgentRecord, file: StoredFile) -> None:
    if file.storage_key != agent_storage_key(agent.id, file.path):
        raise ChannelMediaError("foreign_storage_key")


async def store_inbound_media(
    agent: AgentRecord, attachment: ChatAttachment, data: bytes, *, kind: Literal["image", "file"]
) -> StoredFile:
    """Create one uniquely owned attempt; collisions never replace or own the occupied key."""
    _check_bytes(data, kind)
    storage = get_storage_backend()
    async with guest_write_section(agent.id):
        for _ in range(8):
            path = f"workspace/uploads/zalo/inbound/{uuid4().hex}/{attachment.name}"
            file = StoredFile(
                storage_key=agent_storage_key(agent.id, path),
                path=path,
                name=attachment.name,
                size=len(data),
                sha256=hashlib.sha256(data).hexdigest(),
                kind=kind,
            )
            created = await storage.create_bytes_if_absent(file.storage_key, data, guess_content_type(file.name))
            if created.ok:
                return file
            if not created.conflict:
                raise ChannelMediaError("create_not_confirmed")
    raise ChannelMediaError("filename_collision")


async def read_stored_media(agent: AgentRecord, file: StoredFile) -> bytes:
    _check_owner(agent, file)
    try:
        data = await get_storage_backend().read_bytes_bounded(file.storage_key, file.size)
    except FileNotFoundError, StorageReadLimitExceededError:
        raise ChannelMediaError("stored_file_changed") from None
    if len(data) != file.size or hashlib.sha256(data).hexdigest() != file.sha256:
        raise ChannelMediaError("stored_file_changed")
    _check_bytes(data, file.kind)
    return data


async def materialize_for_guest(agent: AgentRecord, file: StoredFile) -> ChatAttachment:
    data = await read_stored_media(agent, file)
    async with guest_write_section(agent.id):
        await anyio.to_thread.run_sync(publish_guest_file, agent_manager._agent_dir(agent.id), file, data)
        mounted = await anyio.to_thread.run_sync(
            read_guest_file, agent_manager._agent_dir(agent.id), file.path, file.size
        )
        if len(mounted) != file.size or hashlib.sha256(mounted).hexdigest() != file.sha256:
            raise ChannelMediaError("mounted_file_changed")
    return ChatAttachment(path=file.path, name=file.name)


async def snapshot_reply_file(agent: AgentRecord, attachment: ChatAttachment) -> StoredFile:
    if PurePosixPath(attachment.path).parts[0] not in ("workspace", "media"):
        raise ChannelMediaError("foreign_reply_path")
    data = await anyio.to_thread.run_sync(
        read_guest_file, agent_manager._agent_dir(agent.id), attachment.path, MAX_FILE_BYTES
    )
    suffix = PurePosixPath(attachment.name).suffix.lower()
    if suffix == ".gif" or data.startswith((b"GIF87a", b"GIF89a")):
        raise ChannelMediaError("unsupported_image")
    kind: Literal["image", "file"] = "image" if suffix in (".jpg", ".jpeg", ".png", ".webp") else "file"
    _check_bytes(data, kind)
    digest = hashlib.sha256(data).hexdigest()
    path = f"workspace/uploads/zalo/replies/{digest}/{attachment.name}"
    file = StoredFile(
        storage_key=agent_storage_key(agent.id, path),
        path=path,
        name=attachment.name,
        size=len(data),
        sha256=digest,
        kind=kind,
    )
    async with guest_write_section(agent.id):
        created = await get_storage_backend().create_bytes_if_absent(
            file.storage_key, data, guess_content_type(file.name)
        )
        if not created.ok and not created.conflict:
            raise ChannelMediaError("create_not_confirmed")
        if created.conflict:
            _ = await read_stored_media(agent, file)
    return file


async def snapshot_reply_media(agent: AgentRecord, result: str) -> tuple[str, tuple[StoredFile, ...]]:
    media = await anyio.to_thread.run_sync(recover_media, agent_manager._agent_dir(agent.id), result, True)
    files = tuple([await snapshot_reply_file(agent, attachment) for attachment in media.attachments])
    if sum(file.size for file in files) > MAX_FILE_BYTES:
        raise ChannelMediaError("event_too_large")
    return media.text, files
