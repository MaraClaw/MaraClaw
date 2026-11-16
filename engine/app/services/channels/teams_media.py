"""Teams file-download cards and service-bound Bot Framework attachments.

https://learn.microsoft.com/microsoftteams/platform/bots/how-to/bots-filesv4
https://learn.microsoft.com/microsoftteams/platform/bots/how-to/agents-inline-images
"""

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from pathlib import PurePosixPath
from typing import ClassVar
from urllib.parse import urlsplit

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field

from app.core.json_types import JsonObject, json_as_str_or, json_object_from
from app.core.outbound_transport import public_http_client
from app.records.chat_attachment import ChatAttachment
from app.services.channels import submissions
from app.services.channels.media_limits import MediaBudget, MediaTooLargeError, read_capped, too_large_note
from app.services.channels.teams_auth import is_bot_framework_service_url
from app.services.storage import store_agent_upload

FILE_DOWNLOAD = "application/vnd.microsoft.teams.file.download.info"


class TeamsFileInfo(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)

    download_url: str = Field(default="", alias="downloadUrl")
    unique_id: str = Field(default="", alias="uniqueId")


class TeamsAttachment(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)

    content_type: str = Field(alias="contentType")
    content_url: str = Field(default="", alias="contentUrl")
    name: str = ""
    content: TeamsFileInfo = Field(default_factory=TeamsFileInfo)


@dataclass(frozen=True, slots=True)
class TeamsMedia:
    service_url: str
    attachments: tuple[TeamsAttachment, ...]

    def media_ids(self) -> tuple[str, ...]:
        return tuple(item.content.unique_id or item.content_url for item in self.attachments)


def parse_teams_media(activity: JsonObject) -> TeamsMedia:
    raw = activity.get("attachments")
    items = (json_object_from(item) for item in raw) if isinstance(raw, list) else ()
    # HTML is layout, never a download source. Cards/mentions/link previews are not media either.
    attachments = tuple(
        TeamsAttachment.model_validate(item)
        for item in items
        if (kind := json_as_str_or(item.get("contentType"))) != "text/html"
        and (kind == FILE_DOWNLOAD or kind.startswith(("image/", "video/", "application/")))
        and not kind.startswith("application/vnd.microsoft.card.")
    )
    return TeamsMedia(json_as_str_or(activity.get("serviceUrl")), attachments)


async def accept_teams_media(
    event: submissions.ChannelEvent,
    media: TeamsMedia,
    get_token: Callable[[], Awaitable[str | None]],
) -> None:
    if await submissions.is_channel_event_replay(event):
        return
    budget, refs, parts = MediaBudget(), [], [event.text] if event.text else []
    token: str | None = None
    for item in media.attachments[:10]:
        filename = item.name or ("image.png" if item.content_type.startswith("image/") else "file.bin")
        headers = {"Accept-Encoding": "identity"}
        if item.content_type == FILE_DOWNLOAD:
            url = item.content.download_url
            parsed = urlsplit(url)
            # The bot-file contract supplies a preauthorized OneDrive/SharePoint URL, never Graph bearer auth.
            if (
                parsed.scheme != "https"
                or parsed.hostname is None
                or not parsed.hostname.endswith((".sharepoint.com", ".1drv.com"))
                or parsed.port not in (None, 443)
                or parsed.username is not None
                or parsed.password is not None
                or parsed.fragment
                or re.search(r"[\\\s]", url)
            ):
                raise HTTPException(400, "Teams downloadUrl must be a preauthorized OneDrive or SharePoint URL")
            origin = f"https://{parsed.hostname}"
        else:
            url = item.content_url
            # Never trust contentUrl alone with a bot bearer. Bind it to the authenticated service URL and API path.
            if not is_bot_framework_service_url(media.service_url) or not re.fullmatch(
                re.escape(media.service_url.rstrip("/")) + r"/v3/attachments/[^/?#\\\s]+/views/[^/?#\\\s]+", url
            ):
                raise HTTPException(
                    422,
                    "Teams inline media requires a Bot Framework attachment URL on the authenticated serviceUrl; "
                    "send the file in personal chat with supportsFiles enabled",
                )
            origin = f"https://{urlsplit(media.service_url).netloc}"
        try:
            limit = budget.limit_for(filename)
            if item.content_type != FILE_DOWNLOAD:
                if token is None:
                    token = await get_token()
                if token is None or not token:
                    raise HTTPException(422, "Teams attachment download requires a valid Bot Framework bot credential")
                headers["Authorization"] = f"Bearer {token}"
            async with (
                public_http_client(timeout=30, origin=origin) as client,
                client.stream("GET", url, headers=headers) as response,
            ):
                if response.status_code in (401, 403):
                    raise HTTPException(422, "Teams attachment access expired or was denied; attach the file again")
                data = await read_capped(response, limit)
            budget.spend(len(data))
        except MediaTooLargeError as error:
            budget.spend(error.read)
            parts.append(too_large_note(filename, error.limit))
            continue
        _, path, _ = await store_agent_upload(
            event.agent_id,
            filename,
            data,
            content_type=None if item.content_type == FILE_DOWNLOAD else item.content_type,
        )
        refs.append(ChatAttachment(path=path, name=PurePosixPath(path).name))
        parts.append(f"[file: {path}]")
    if len(media.attachments) > 10:
        parts.append("[files skipped: maximum 10 attachments per message]")
    content = "\n".join(parts)
    await submissions.accept_channel_event(
        replace(event, attachments=tuple(refs)), content=content, saved_content=content
    )
