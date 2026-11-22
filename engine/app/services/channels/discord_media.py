"""Discord attachment CDN intake; native receipt semantics stay with submissions.

Contract: https://docs.discord.com/developers/reference#signed-attachment-cdn-urls
"""

import re
from dataclasses import replace
from pathlib import PurePosixPath
from typing import ClassVar
from urllib.parse import quote

import httpx
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field

from app.core.json_types import json_as_int, json_object_from_response
from app.core.outbound_transport import public_http_client
from app.records.chat_attachment import ChatAttachment
from app.services.channels import submissions
from app.services.channels.media_limits import (
    MediaBudget,
    MediaTooLargeError,
    limit_attachments,
    read_capped,
    skipped_attachments_note,
    too_large_note,
)
from app.services.channels.stream_source import authorize_stream_source
from app.services.storage import store_agent_upload


class DiscordAttachment(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, from_attributes=True)

    id: str | int
    filename: str
    url: str
    content_type: str | None = None
    size: int = Field(default=0, ge=0)


async def defer_discord_media(event: submissions.ChannelEvent, interaction_id: str, token: str) -> None:
    if await submissions.is_channel_event_replay(event):
        return
    if not interaction_id.isdecimal() or not token or len(token) > 2048:
        raise HTTPException(400, "Discord attachment interaction requires its native ID and continuation token")
    # The official callback API defers before media I/O; the synchronous HTTP intake then returns empty 202.
    try:
        async with public_http_client(timeout=2, origin="https://discord.com") as client:
            response = await client.post(
                f"https://discord.com/api/v10/interactions/{interaction_id}/{quote(token, safe='')}/callback",
                json={"type": 5},
            )
        if response.status_code == 400 and json_as_int(json_object_from_response(response).get("code")) == 40060:
            return
        response.raise_for_status()
    except httpx.HTTPError, ValueError:
        raise HTTPException(502, "Discord could not defer this attachment interaction; please retry") from None


async def accept_discord_media(event: submissions.ChannelEvent, attachments: tuple[DiscordAttachment, ...]) -> None:
    if await submissions.is_channel_event_replay(event):
        return
    budget = MediaBudget()
    refs: list[ChatAttachment] = []
    parts = [event.text] if event.text else []
    retrieved, skipped = limit_attachments(attachments)
    for attachment in retrieved:
        # Only Discord's documented CDN attachment paths; no bot credential is needed on signed URLs.
        match = re.fullmatch(
            r"https://(cdn\.discordapp\.com|media\.discordapp\.net)(/attachments/[0-9]+/[0-9]+/[^/?#\\\s]+)(?:\?[^#\\\s]*)?",
            attachment.url,
        )
        if match is None or match[2].split("/")[3] != str(attachment.id):
            raise HTTPException(400, "Discord attachment must use its official CDN resource URL")
        try:
            limit = budget.limit_for(attachment.filename)
            if attachment.size > limit:
                raise MediaTooLargeError(limit)
            if event.stream_source is not None:
                await authorize_stream_source(event.stream_source)
            async with (
                public_http_client(timeout=30, origin=f"https://{match[1]}") as client,
                client.stream("GET", attachment.url, headers={"Accept-Encoding": "identity"}) as response,
            ):
                data = await read_capped(response, limit)
            budget.spend(len(data))
        except MediaTooLargeError as error:
            budget.spend(error.read)
            parts.append(too_large_note(attachment.filename, error.limit))
            continue
        if event.stream_source is not None:
            await authorize_stream_source(event.stream_source)
        _, path, _ = await store_agent_upload(
            event.agent_id, attachment.filename, data, content_type=attachment.content_type
        )
        refs.append(ChatAttachment(path=path, name=PurePosixPath(path).name))
        parts.append(f"[file: {path}]")
    if skipped:
        parts.append(skipped_attachments_note(skipped))
    content = "\n".join(parts)
    await submissions.accept_channel_event(
        replace(event, attachments=tuple(refs)), content=content, saved_content=content
    )
