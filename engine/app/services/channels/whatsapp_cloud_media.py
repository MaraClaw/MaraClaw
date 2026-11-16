import re
from dataclasses import replace
from pathlib import PurePosixPath
from typing import override
from urllib.parse import quote

import httpx
from fastapi import HTTPException

from app.core.json_types import JsonObject, json_as_str_or, json_loads_object
from app.core.outbound_policy import OutboundPolicyError
from app.core.outbound_transport import public_http_client
from app.records.channel_config import ChannelConfigRecord
from app.records.chat_attachment import ChatAttachment
from app.services.channels.media_limits import MediaBudget, MediaTooLargeError, read_capped, too_large_note
from app.services.channels.submissions import ChannelEvent
from app.services.storage import store_agent_upload


class _MediaURL(httpx.URL):
    @override
    def __str__(self) -> str:
        return f"https://{self.host}/[redacted]"

    @override
    def __repr__(self) -> str:
        return f"URL('{self}')"


async def enrich_media(config: ChannelConfigRecord, event: ChannelEvent, media: JsonObject) -> tuple[ChannelEvent, str]:
    media_id = json_as_str_or(media.get("id"))
    default_name = "photo.jpg" if event.kind == "image" else "video.mp4" if event.kind == "video" else "file.bin"
    filename = json_as_str_or(media.get("filename")) or default_name
    version = json_as_str_or((config.extra_config or {}).get("api_version"), "v23.0")
    if re.fullmatch(r"v[0-9]+\.[0-9]+", version) is None:
        raise HTTPException(503, "WhatsApp API version is invalid")
    headers = {"Authorization": f"Bearer {config.app_secret or ''}"}
    budget = MediaBudget()
    try:
        async with (
            public_http_client(timeout=30, origin="https://graph.facebook.com") as client,
            client.stream(
                "GET",
                f"https://graph.facebook.com/{version}/{quote(media_id, safe='')}",
                headers=headers,
                params={"phone_number_id": config.app_id or ""},
            ) as response,
        ):
            metadata = json_loads_object(await read_capped(response, 64 * 1024))
        url = httpx.URL(json_as_str_or(metadata.get("url")))
        if (
            url.scheme != "https"
            or url.host != "lookaside.fbsbx.com"
            or url.port not in (None, 443)
            or url.userinfo
            or url.fragment
        ):
            raise OutboundPolicyError("Untrusted WhatsApp media destination")
        limit = budget.limit_for(filename)
        async with public_http_client(timeout=60, origin="https://lookaside.fbsbx.com") as client:
            request = client.build_request("GET", "https://lookaside.fbsbx.com/[redacted]", headers=headers)
            request.url = _MediaURL(url)
            response = await client.send(request, stream=True, follow_redirects=False)
            try:
                data = await read_capped(response, limit)
            finally:
                await response.aclose()
    except MediaTooLargeError as exc:
        return event, f"{event.text}\n{too_large_note(filename, exc.limit)}".strip()
    except httpx.HTTPError:
        raise HTTPException(503, "WhatsApp media download failed") from None
    _, path, _ = await store_agent_upload(
        config.agent_id, filename, data, content_type=json_as_str_or(media.get("mime_type")) or None
    )
    reference = ChatAttachment(path=path, name=PurePosixPath(path).name)
    return replace(event, attachments=(reference,)), f"{event.text}\n[file:{path}]".strip()
