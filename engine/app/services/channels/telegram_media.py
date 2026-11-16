from dataclasses import replace
from pathlib import PurePosixPath
from urllib.parse import quote

import httpx
from fastapi import HTTPException

from app.core.json_types import json_as_str_or, json_loads_object, json_object_from
from app.core.outbound_transport import public_http_client
from app.records.channel_config import ChannelConfigRecord
from app.records.chat_attachment import ChatAttachment
from app.schemas.telegram import TelegramMedia, TelegramMessage
from app.services.channels.media_limits import MediaBudget, MediaTooLargeError, read_capped, too_large_note
from app.services.channels.submissions import ChannelEvent
from app.services.channels.telegram_outbound import _TelegramBotURL
from app.services.storage import store_agent_upload


def message_media(message: TelegramMessage) -> tuple[str, TelegramMedia] | None:
    if message.document is not None:
        return "document", message.document
    if message.video is not None:
        return "video", message.video
    if message.photo:
        return "photo", message.photo[-1]
    return None


async def enrich_media(
    config: ChannelConfigRecord, event: ChannelEvent, message: TelegramMessage
) -> tuple[ChannelEvent, str]:
    selected = message_media(message)
    if selected is None:
        return event, event.text
    kind, media = selected
    filename = media.file_name or ("photo.jpg" if kind == "photo" else "video.mp4" if kind == "video" else "file.bin")
    budget = MediaBudget()
    token = config.app_secret or ""
    try:
        limit = min(budget.limit_for(filename), 20 * 1024 * 1024)
        if media.file_size is not None and media.file_size > limit:
            raise MediaTooLargeError(limit)
        async with public_http_client(timeout=30, origin="https://api.telegram.org") as client:
            request = client.build_request(
                "GET", "https://api.telegram.org/bot[redacted]/getFile", params={"file_id": media.file_id}
            )
            request.url = _TelegramBotURL(
                f"https://api.telegram.org/bot{token}/getFile?file_id={quote(media.file_id, safe='')}"
            )
            response = await client.send(request, stream=True, follow_redirects=False)
            try:
                body = json_loads_object(await read_capped(response, 64 * 1024))
            finally:
                await response.aclose()
            file_path = json_as_str_or(json_object_from(body.get("result")).get("file_path"))
            if (
                body.get("ok") is not True
                or not file_path
                or any(part in ("", ".", "..") for part in file_path.split("/"))
                or "\\" in file_path
            ):
                raise HTTPException(503, "Telegram media resource is unavailable")
            request = client.build_request("GET", "https://api.telegram.org/file/[redacted]")
            request.url = _TelegramBotURL(f"https://api.telegram.org/file/bot{token}/{quote(file_path, safe='/')}")
            response = await client.send(request, stream=True, follow_redirects=False)
            try:
                data = await read_capped(response, limit)
            finally:
                await response.aclose()
    except MediaTooLargeError as exc:
        return event, f"{event.text}\n{too_large_note(filename, exc.limit)}".strip()
    except httpx.HTTPError:
        raise HTTPException(503, "Telegram media download failed") from None
    _, path, _ = await store_agent_upload(config.agent_id, filename, data, content_type=media.mime_type)
    reference = ChatAttachment(path=path, name=PurePosixPath(path).name)
    return replace(event, attachments=(reference,)), f"{event.text}\n[file:{path}]".strip()
