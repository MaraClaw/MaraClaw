"""DingTalk service for sending messages via Open API."""

import json

import httpx

from app.core.json_types import JsonObject
from app.core.logging import logger


async def get_dingtalk_access_token(app_id: str, app_secret: str) -> JsonObject:
    """Get DingTalk access_token using app_id and app_secret.

    API: https://open.dingtalk.com/document/orgapp/obtain-access_token
    """
    url = "https://oapi.dingtalk.com/gettoken"
    params = {
        "appkey": app_id,
        "appsecret": app_secret,
    }

    async with httpx.AsyncClient(timeout=10) as client:
        try:
            resp = await client.get(url, params=params)
            data = resp.json()

            if data.get("errcode") == 0:
                return {"access_token": data.get("access_token"), "expires_in": data.get("expires_in")}
            logger.error(f"[DingTalk] Failed to get access_token: {data}")
            return {"errcode": data.get("errcode"), "errmsg": data.get("errmsg")}
        except Exception as e:
            logger.error(f"[DingTalk] Network error getting access_token: {e}")
            return {"errcode": -1, "errmsg": str(e)}


async def send_dingtalk_v1_robot_oto_message(
    app_id: str,
    app_secret: str,
    user_ids: list[str],
    message: str,
    msg_type: str = "text",
    robot_code: str | None = None,
) -> JsonObject:
    """Send single chat messages via Robot using modern v1.0 API (RECOMMENDED).

    API: /v1.0/robot/oToMessages/batchSend
    Docs: https://open.dingtalk.com/document/orgapp/batch-send-single-chat-messages
    """
    token_result = await get_dingtalk_access_token(app_id, app_secret)
    access_token = token_result.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        return token_result

    url = "https://api.dingtalk.com/v1.0/robot/oToMessages/batchSend"
    headers = {"x-acs-dingtalk-access-token": access_token, "Content-Type": "application/json"}

    # Map text to standard templates
    if msg_type == "markdown":
        msg_key = "sampleMarkdown"
        msg_param = json.dumps({"title": "Notification", "text": message})
    else:
        msg_key = "sampleText"
        msg_param = json.dumps({"content": message})

    payload = {"robotCode": robot_code or app_id, "userIds": user_ids, "msgKey": msg_key, "msgParam": msg_param}

    async with httpx.AsyncClient(timeout=30) as client:
        try:
            resp = await client.post(url, headers=headers, json=payload)
            data = resp.json()
            if resp.status_code == 200:
                logger.info(f"[DingTalk] Robot v1.0 OTO batch message sent to {user_ids}")
                return {"errcode": 0, "processQueryKey": data.get("processQueryKey")}
            logger.error(f"[DingTalk] Failed to send v1.0 OTO message: {data}")
            return {"errcode": resp.status_code, "errmsg": str(data)}
        except Exception as e:
            logger.error(f"[DingTalk] Network error sending v1.0 OTO message: {e}")
            return {"errcode": -1, "errmsg": str(e)}


async def send_dingtalk_corp_conversation(
    app_id: str,
    app_secret: str,
    user_id: str,
    msg_body: JsonObject,
    agent_id: str,
) -> JsonObject:
    """Send a work notification.

    API: https://open.dingtalk.com/document/orgapp/send-asynchronous-messages-to-users
    """
    token_result = await get_dingtalk_access_token(app_id, app_secret)
    access_token = token_result.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        return token_result

    url = "https://oapi.dingtalk.com/topapi/message/corpconversation/asyncsend_v2"
    params = {"access_token": access_token}

    payload = {
        "agent_id": agent_id,
        "userid_list": user_id,
        "msg": msg_body,
    }

    async with httpx.AsyncClient(timeout=30) as client:
        try:
            resp = await client.post(url, params=params, json=payload)
            data = resp.json()
            if data.get("errcode") == 0:
                return data
            logger.error(f"[DingTalk] Failed to send corp conversation: {data}")
            return data
        except Exception as e:
            logger.error(f"[DingTalk] Network error sending corp conversation: {e}")
            return {"errcode": -1, "errmsg": str(e)}


async def send_dingtalk_message(
    app_id: str,
    app_secret: str,
    user_id: str,
    message: str,
    agent_id: str | None = None,
    use_robot: bool = True,
    msg_type: str = "text",
) -> JsonObject:
    """Unified message sending method.

    Default behavior is sending via Robot OTO (Private Message) using v1.0 API.
    """
    if use_robot:
        # Use v1.0 API for private chat
        return await send_dingtalk_v1_robot_oto_message(
            app_id=app_id, app_secret=app_secret, user_ids=[user_id], message=message, msg_type=msg_type
        )
    # Use Work Notification
    msg_body: JsonObject = {
        "msgtype": msg_type,
        msg_type: {"content": message} if msg_type == "text" else {"title": "Notification", "text": message},
    }
    if not agent_id:
        agent_id = app_id
    return await send_dingtalk_corp_conversation(app_id, app_secret, user_id, msg_body, agent_id)


async def download_dingtalk_media(app_id: str, app_secret: str, download_code: str) -> bytes | None:
    """Download a media file from DingTalk using a downloadCode.

    Convenience wrapper that delegates to the stream module's download helper.
    Returns raw file bytes on success, or None on failure.

    Args:
        app_id: DingTalk app key (robotCode).
        app_secret: DingTalk app secret.
        download_code: The downloadCode from the incoming message payload.
    """
    from app.services.dingtalk_stream import download_dingtalk_media as _download

    return await _download(app_id, app_secret, download_code)
