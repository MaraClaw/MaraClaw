"""iLink CDN descriptors and bounded AES-128-ECB media downloads."""

from __future__ import annotations

import base64
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Final
from urllib.parse import quote
from uuid import UUID

from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad

from app.core.json_types import JsonObject, json_as_str_or, json_object_from
from app.core.outbound_policy import ensure_same_origin
from app.core.outbound_transport import public_http_client
from app.records.chat_attachment import ChatAttachment
from app.services.storage import store_agent_upload

from .media_limits import (
    MediaBudget,
    MediaTooLargeError,
    limit_attachments,
    read_capped,
    skipped_attachments_note,
    too_large_note,
)

WECHAT_CDN_ORIGIN: Final = "https://novac2c.cdn.weixin.qq.com"


@dataclass(frozen=True, slots=True)
class WeChatMedia:
    kind: str
    filename: str
    url: str
    key: bytes

    def fingerprint_id(self) -> str:
        payload = json.dumps([self.kind, self.filename, self.url, self.key.hex()], separators=(",", ":"))
        return hashlib.sha256(payload.encode()).hexdigest()


def parse_wechat_media(item: JsonObject) -> WeChatMedia | None:
    match item.get("type"):
        case 2:
            kind, field, filename = "image", "image_item", "image.jpg"
        case 4:
            kind, field, filename = "file", "file_item", "file.bin"
        case 5:
            kind, field, filename = "video", "video_item", "video.mp4"
        case _:
            return None
    descriptor = json_object_from(item.get(field))
    if not descriptor:
        return None
    media = json_object_from(descriptor.get("media"))
    query = json_as_str_or(media.get("encrypt_query_param"))
    url = json_as_str_or(media.get("full_url"))
    if not url:
        if not query:
            raise ValueError("WeChat media CDN descriptor is required")
        url = WECHAT_CDN_ORIGIN + "/c2c/download?encrypted_query_param=" + quote(query, safe="")
    key_hex = json_as_str_or(descriptor.get("aeskey")) if kind == "image" else ""
    key_base64 = json_as_str_or(media.get("aes_key"))
    key = b""
    if key_hex:
        if re.fullmatch(r"[0-9a-fA-F]{32}", key_hex) is None:
            raise ValueError("WeChat image AES key must contain 16 bytes")
        key = bytes.fromhex(key_hex)
    elif key_base64:
        decoded = base64.b64decode(key_base64, validate=True)
        key = bytes.fromhex(decoded.decode("ascii")) if re.fullmatch(rb"[0-9a-fA-F]{32}", decoded) else decoded
        if len(key) != 16:
            raise ValueError("WeChat media AES key must contain 16 bytes")
    elif kind != "image":
        raise ValueError("WeChat file and video media require an AES key")
    if kind == "file":
        filename = json_as_str_or(descriptor.get("file_name")) or filename
    return WeChatMedia(kind, filename, url, key)


async def store_wechat_media(agent_id: UUID, media: tuple[WeChatMedia, ...]) -> tuple[str, tuple[ChatAttachment, ...]]:
    budget = MediaBudget()
    lines: list[str] = []
    attachments: list[ChatAttachment] = []
    retrieved, skipped = limit_attachments(media)
    for item in retrieved:
        try:
            limit = budget.limit_for("image.jpg" if item.kind == "image" else item.filename)
            ensure_same_origin(WECHAT_CDN_ORIGIN, item.url)
            async with (
                public_http_client(timeout=30, origin=WECHAT_CDN_ORIGIN) as client,
                client.stream("GET", item.url, headers={"Accept-Encoding": "identity"}) as response,
            ):
                data = await read_capped(response, limit + (16 if item.key else 0))
            if item.key:
                data = unpad(AES.new(item.key, AES.MODE_ECB).decrypt(data), AES.block_size)
            if len(data) > limit:
                raise MediaTooLargeError(limit, len(data))
        except MediaTooLargeError as error:
            budget.spend(error.read)
            lines.append(too_large_note(item.filename, error.limit))
            continue
        budget.spend(len(data))
        _, path, _ = await store_agent_upload(agent_id, item.filename, data)
        attachments.append(ChatAttachment(path=path, name=PurePosixPath(path).name))
        lines.append(f"[file:{path}]")
    if skipped:
        lines.append(skipped_attachments_note(skipped))
    return "\n".join(lines), tuple(attachments)
