"""Bounded WeCom resource downloads and SDK attachment decryption."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import re
from dataclasses import dataclass, replace
from pathlib import PurePosixPath
from typing import Final

from fastapi import HTTPException
from wecom_aibot_sdk.crypto import decrypt_file, extract_filename

from app.core.json_types import JsonObject, http_header, json_as_str_or, json_loads_object, json_object_from
from app.core.outbound_policy import ensure_same_origin
from app.core.outbound_transport import public_http_client
from app.records.channel_config import ChannelConfigRecord
from app.records.chat_attachment import ChatAttachment
from app.services.storage import store_agent_upload
from app.services.wecom_service import get_wecom_access_token

from .media_limits import (
    MediaBudget,
    MediaDeadlineError,
    MediaTooLargeError,
    deadline_note,
    limit_attachments,
    read_capped,
    skipped_attachments_note,
    too_large_note,
)
from .stream_source import authorize_stream_source
from .submissions import ChannelEvent, accept_channel_event, is_channel_event_replay

WECOM_RESOURCE_ORIGIN: Final = "https://qyapi.weixin.qq.com"
WECOM_STREAM_ORIGIN: Final = "https://ww-aibot-img-1258476243.cos.ap-guangzhou.myqcloud.com"


@dataclass(frozen=True, slots=True)
class WeComMedia:
    kind: str
    filename: str
    media_id: str = ""
    url: str = ""
    aes_key: str = ""

    def fingerprint_id(self) -> str:
        # Signed URLs and attachment keys must never become persisted native IDs.
        if self.media_id:
            return self.media_id
        payload = json.dumps([self.kind, self.filename, self.url, self.aes_key], separators=(",", ":"))
        return hashlib.sha256(payload.encode()).hexdigest()


def stream_media(body: JsonObject) -> tuple[str, tuple[WeComMedia, ...]]:
    """Parse the SDK's image/file and mixed text/image descriptors without retaining secrets in text."""
    kind = json_as_str_or(body.get("msgtype"))
    parts = [body]
    if kind == "mixed":
        raw = json_object_from(body.get("mixed")).get("msg_item")
        parts = [json_object_from(item) for item in raw] if isinstance(raw, list) else []
    texts: list[str] = []
    media: list[WeComMedia] = []
    for part in parts:
        part_kind = json_as_str_or(part.get("msgtype")) or json_as_str_or(part.get("type"))
        text = json_as_str_or(json_object_from(part.get("text")).get("content")).strip()
        if text:
            texts.append(text)
        if part_kind in {"image", "file", "video"}:
            item = json_object_from(part.get(part_kind))
            url, key = json_as_str_or(item.get("url")), json_as_str_or(item.get("aeskey"))
            if not url or not key:
                raise HTTPException(400, "WeCom attachment URL and AES key are required")
            name = (
                json_as_str_or(item.get("name"))
                or {"image": "image.jpg", "video": "video.mp4", "file": "file.bin"}[part_kind]
            )
            media.append(WeComMedia(part_kind, name, url=url, aes_key=key))
    return "\n".join(texts), tuple(media)


async def _download(media: WeComMedia, config: ChannelConfigRecord, budget: MediaBudget) -> tuple[bytes, str]:
    name = media.filename
    _ = budget.limit_for("image.jpg" if media.kind == "image" else name)
    if media.media_id:
        token_data = await get_wecom_access_token(config.app_id or "", config.app_secret or "")
        token = json_as_str_or(token_data.get("access_token"))
        if not token:
            raise HTTPException(503, "WeCom media token unavailable")
        url = WECOM_RESOURCE_ORIGIN + "/cgi-bin/media/get"
        params = {"access_token": token, "media_id": media.media_id}
        origin = WECOM_RESOURCE_ORIGIN
    else:
        ensure_same_origin(WECOM_STREAM_ORIGIN, media.url)
        url, params, origin = media.url, {}, WECOM_STREAM_ORIGIN
    with io.BytesIO() as output:
        async with public_http_client(timeout=30, origin=origin) as client:
            total_size: int | None = None
            while True:
                offset = output.tell()
                headers = {"Accept-Encoding": "identity"}
                if media.media_id:
                    headers["Range"] = f"bytes={offset}-{offset + 20 * 1024 * 1024 - 1}"
                async with client.stream("GET", url, params=params, headers=headers) as response:
                    if offset and response.status_code != 206:
                        raise HTTPException(503, "WeCom media range continuation was not partial")
                    header_name = extract_filename(http_header(response.headers, "content-disposition"))
                    if header_name != "unknown_file":
                        name = header_name
                    limit = budget.limit_for("image.jpg" if media.kind == "image" else name)
                    content_range = None
                    if media.media_id and response.status_code == 206:
                        content_range = re.fullmatch(
                            r"bytes (\d+)-(\d+)/(\d+)", http_header(response.headers, "content-range")
                        )
                        if (
                            content_range is None
                            or int(content_range[1]) != offset
                            or int(content_range[2]) < offset
                            or int(content_range[2]) - offset >= 20 * 1024 * 1024
                            or (total_size is not None and total_size != int(content_range[3]))
                        ):
                            raise HTTPException(503, "Invalid WeCom media range response")
                        total_size = int(content_range[3])
                        if int(content_range[3]) > limit:
                            raise MediaTooLargeError(limit, offset)
                    try:
                        part = await read_capped(response, limit - offset + (32 if media.aes_key else 0), budget)
                    except MediaTooLargeError as error:
                        raise MediaTooLargeError(limit, offset + error.read) from error
                    if (
                        media.media_id
                        and "json" in http_header(response.headers, "content-type").lower()
                        and json_loads_object(part).get("errcode") not in (None, 0)
                    ):
                        raise HTTPException(503, "WeCom media resource request failed")
                    if content_range is not None and (
                        int(content_range[2]) - offset + 1 != len(part)
                        or int(content_range[2]) >= int(content_range[3])
                    ):
                        raise HTTPException(503, "Invalid WeCom media range length")
                    _ = output.write(part)
                    if content_range is None or output.tell() == int(content_range[3]):
                        break
        data = output.getvalue()
    if media.aes_key:
        if len(base64.b64decode(media.aes_key + "=" * (-len(media.aes_key) % 4), validate=True)) != 32:
            raise ValueError("WeCom attachment AES key must contain 32 bytes")
        data = decrypt_file(data, media.aes_key)
    if len(data) > limit:
        raise MediaTooLargeError(limit, len(data))
    return data, name


async def accept_wecom_media(
    native: ChannelEvent,
    config: ChannelConfigRecord,
    media: tuple[WeComMedia, ...],
    *,
    budget: MediaBudget | None = None,
) -> None:
    """Authorize the original before I/O, then enrich only attachment refs and accepted content."""
    if await is_channel_event_replay(native):
        return
    content = native.text
    attachments: list[ChatAttachment] = []
    if budget is None:
        budget = MediaBudget() if native.stream_source is not None else MediaBudget.for_acknowledgement()
    retrieved, skipped = limit_attachments(media)
    for item in retrieved:
        if native.stream_source is not None:
            await authorize_stream_source(native.stream_source)
        try:
            async with budget.window():
                data, name = await _download(item, config, budget)
            if native.stream_source is not None:
                await authorize_stream_source(native.stream_source)
            budget.check()
        except MediaTooLargeError as error:
            budget.spend(error.read)
            content = f"{content}\n{too_large_note(item.filename, error.limit)}".strip()
            continue
        except MediaDeadlineError:
            content = f"{content}\n{deadline_note(item.filename)}".strip()
            continue
        budget.spend(len(data))
        _, path, _ = await store_agent_upload(config.agent_id, name, data)
        attachments.append(ChatAttachment(path=path, name=PurePosixPath(path).name))
        content = f"{content}\n[file:{path}]".strip()
    if skipped:
        content = f"{content}\n{skipped_attachments_note(skipped)}".strip()
    await accept_channel_event(replace(native, attachments=tuple(attachments)), content=content, saved_content=content)
