"""Google Chat uploaded media, using resourceName and app authentication only.

https://developers.google.com/workspace/chat/api/reference/rest/v1/media/download
The downloadUri/thumbnailUri fields are human viewer URLs, not app download APIs.
"""

import re
from dataclasses import replace
from pathlib import PurePosixPath
from typing import ClassVar

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field

from app.core.outbound_transport import public_http_client
from app.records.channel_config import ChannelConfigRecord
from app.records.chat_attachment import ChatAttachment
from app.services.channels import google_chat, submissions
from app.services.channels.media_limits import (
    MediaBudget,
    MediaTooLargeError,
    limit_attachments,
    read_capped,
    skipped_attachments_note,
    too_large_note,
)
from app.services.storage import store_agent_upload


class AttachmentDataRef(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)

    resource_name: str = Field(default="", alias="resourceName")


class DriveDataRef(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)

    drive_file_id: str = Field(default="", alias="driveFileId")


class GoogleChatAttachment(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)

    name: str = ""
    content_name: str = Field(default="file.bin", alias="contentName")
    content_type: str | None = Field(default=None, alias="contentType")
    attachment_data_ref: AttachmentDataRef = Field(default_factory=AttachmentDataRef, alias="attachmentDataRef")
    drive_data_ref: DriveDataRef | None = Field(default=None, alias="driveDataRef")
    source: str = ""


async def accept_google_chat_media(
    event: submissions.ChannelEvent,
    config: ChannelConfigRecord,
    attachments: tuple[GoogleChatAttachment, ...],
) -> None:
    if await submissions.is_channel_event_replay(event):
        return
    if any(item.drive_data_ref is not None or item.source == "DRIVE_FILE" for item in attachments):
        raise HTTPException(
            422,
            "Google Drive attachments require a Drive-authorized credential with drive.readonly and file access; "
            "the configured chat.bot service account cannot read the sender's Drive. Upload the file directly to Chat",
        )
    sa_info = google_chat._service_account_info(config)
    if sa_info is None or not sa_info:
        raise HTTPException(422, "Google Chat media requires service_account_json with the chat.bot scope")
    retrieved, skipped = limit_attachments(attachments)
    # Validate every selected resource before exchanging credentials or downloading anything.
    for item in retrieved:
        resource = item.attachment_data_ref.resource_name
        if not re.fullmatch(r"spaces/[\w-]+/messages/[\w.~-]+/attachments/[\w.~-]+", resource, flags=re.ASCII) or any(
            segment in (".", "..") for segment in resource.split("/")
        ):
            raise HTTPException(422, "Google Chat attachment requires its attachmentDataRef.resourceName")
    budget, refs, parts = MediaBudget(), [], [event.text] if event.text else []
    token: str | None = None
    for item in retrieved:
        try:
            limit = budget.limit_for(item.content_name)
            if token is None:
                token = await google_chat._service_account_access_token(
                    sa_info, [google_chat.CHAT_BOT_SCOPE], pinned=True
                )
            resource = item.attachment_data_ref.resource_name
            async with (
                public_http_client(timeout=30, origin="https://chat.googleapis.com") as client,
                client.stream(
                    "GET",
                    f"https://chat.googleapis.com/v1/media/{resource}",
                    params={"alt": "media"},
                    headers={"Authorization": f"Bearer {token}", "Accept-Encoding": "identity"},
                ) as response,
            ):
                if response.status_code in (401, 403):
                    raise HTTPException(
                        422,
                        "Google Chat denied media access; verify service_account_json, chat.bot scope and space access",
                    )
                data = await read_capped(response, limit)
            budget.spend(len(data))
        except MediaTooLargeError as error:
            budget.spend(error.read)
            parts.append(too_large_note(item.content_name, error.limit))
            continue
        _, path, _ = await store_agent_upload(event.agent_id, item.content_name, data, content_type=item.content_type)
        refs.append(ChatAttachment(path=path, name=PurePosixPath(path).name))
        parts.append(f"[file: {path}]")
    if skipped:
        parts.append(skipped_attachments_note(skipped))
    content = "\n".join(parts)
    await submissions.accept_channel_event(
        replace(event, attachments=tuple(refs)), content=content, saved_content=content
    )
