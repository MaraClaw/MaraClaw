from collections.abc import Awaitable, Callable
from dataclasses import replace
from pathlib import PurePosixPath
from typing import assert_never

import anyio
from fastapi import HTTPException

from app.db.session import connection_ctx, get_connection
from app.records.chat_attachment import ChatAttachment
from app.services.channels.media_limits import MediaBudget, too_large_note
from app.services.channels.personal_burst_outcome import PersonalRejected
from app.services.channels.submissions import (
    ChannelEvent,
    ChannelEventConflictError,
    accept_channel_event,
    is_channel_event_replay,
)
from app.services.channels.whatsapp_personal_media_transport import MediaTransferError
from app.services.channels.whatsapp_personal_protocol import Message
from app.services.storage import store_agent_upload
from app.services.storage_runtime.agent_files import sanitize_filename
from app.services.storage_runtime.facade import get_storage_backend


async def accept_media(
    event: ChannelEvent, message: Message, download: Callable[[Message, int], Awaitable[bytes]] | None
) -> None:
    offer = message.media
    if offer is None or download is None or get_connection() is not None:
        raise MediaTransferError("invalid")
    async with connection_ctx():
        if await is_channel_event_replay(event):
            return
    filename = sanitize_filename(offer.filename)
    budget = MediaBudget()
    limit = min(budget.limit_for(filename), 10 * 1024 * 1024 if offer.kind == "image" else 50 * 1024 * 1024)
    try:
        if offer.size > limit:
            raise MediaTransferError("too_large")
        data = await download(message, limit)
        if len(data) > limit:
            raise MediaTransferError("too_large")
    except MediaTransferError as error:
        match error.code:
            case "too_large":
                pass
            case "expired" | "download_failed" | "forbidden" | "invalid" | "digest_mismatch":
                raise HTTPException(409, "Personal WhatsApp media unavailable") from error
            case unreachable:
                assert_never(unreachable)
        content = "\n".join(filter(None, (event.text, too_large_note(filename, limit))))
        await accept_channel_event(event, content=content, saved_content=content)
        return
    async with connection_ctx():
        if await is_channel_event_replay(event):
            return
    with anyio.CancelScope(shield=True):
        key, path, _ = await store_agent_upload(event.agent_id, filename, data, content_type=offer.content_type)
        ref = ChatAttachment(path=path, name=PurePosixPath(path).name)
        content = "\n".join(filter(None, (event.text, f"[file: {path}]")))
        try:
            outcome = await accept_channel_event(
                replace(event, attachments=(ref,)), content=content, saved_content=content
            )
        except HTTPException, ChannelEventConflictError:
            await get_storage_backend().delete(key)
            raise
        if outcome.replayed or isinstance(outcome.personal, PersonalRejected):
            await get_storage_backend().delete(key)
