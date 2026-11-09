"""DingTalk's API exchange and uncredentialed temporary attachment downloads."""

import httpx

from app.core.json_types import json_as_str, json_object_from_response
from app.core.logging import logger
from app.core.outbound_transport import public_http_client
from app.services.channels.media_limits import read_capped


async def get_media_download_url(access_token: str, download_code: str, robot_code: str) -> str | None:
    async with public_http_client(timeout=10, origin="https://api.dingtalk.com") as client:
        response = await client.post(
            "https://api.dingtalk.com/v1.0/robot/messageFiles/download",
            headers={"x-acs-dingtalk-access-token": access_token},
            json={"downloadCode": download_code, "robotCode": robot_code},
        )
    if response.is_server_error:
        _ = response.raise_for_status()
    try:
        data = json_object_from_response(response)
    except ValueError:
        logger.error("[DingTalk] Invalid download URL response")
        return None
    url = json_as_str(data.get("downloadUrl"))
    if url not in (None, ""):
        return url
    logger.error("[DingTalk] Download URL lookup refused (status=%s)", response.status_code)
    return None


async def download_file(url: str, max_bytes: int) -> bytes | None:
    try:
        async with (
            public_http_client(timeout=60, follow_redirects=True) as client,
            client.stream("GET", url) as response,
        ):
            return await read_capped(response, max_bytes)
    except httpx.HTTPStatusError as error:
        if error.response.is_server_error:
            raise
        logger.error("[DingTalk] File download refused (status=%s)", error.response.status_code)
        return None
