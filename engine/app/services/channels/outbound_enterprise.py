import json
from uuid import uuid4

import httpx

from app.core.json_types import json_as_str_or, json_object_from
from app.records.outbound_plan import OutboundChunk, OutboundProvider
from app.services.channels.outbound_errors import response_body
from app.services.channels.outbound_plan import ConfiguredOutbound
from app.services.channels.outbound_transport import require_ack


async def _send_teams_activity(
    client: httpx.AsyncClient, outbound: ConfiguredOutbound, chunk: OutboundChunk
) -> httpx.Response:
    """Post one activity with the shared bot token; a 401 drops the rejected token and retries once."""
    from app.api.teams import _drop_rejected_teams_token, _get_teams_access_token

    target, config = outbound.target, outbound.config

    async def post(token: str) -> httpx.Response:
        return await client.post(
            f"{target.endpoint}/v3/conversations/{target.destination}/activities",
            headers={"Authorization": f"Bearer {token}"},
            json={"type": "message", "text": chunk.text, "conversation": {"id": target.destination}},
        )

    token = await _get_teams_access_token(config)
    if token is None or not token:
        raise ValueError("Teams credentials are unavailable")
    response = await post(token)
    if response.status_code != 401:
        return response
    await _drop_rejected_teams_token(config, token)
    renewed = await _get_teams_access_token(config)
    if renewed is None or not renewed:
        raise ValueError("Teams credentials are unavailable")
    return await post(renewed)


async def send_enterprise_chunk(client: httpx.AsyncClient, outbound: ConfiguredOutbound, chunk: OutboundChunk) -> None:
    target, config = outbound.target, outbound.config
    app_id, app_secret = config.app_id or "", config.app_secret or ""
    match target.provider:
        case OutboundProvider.MICROSOFT_TEAMS:
            response = await _send_teams_activity(client, outbound, chunk)
            body = response_body(response, target.provider)
            require_ack(response, target.provider, bool(body.get("id")), "missing_ack")
        case OutboundProvider.WECOM:
            from app.services.wecom_service import get_wecom_access_token

            if not app_id or not app_secret:
                raise ValueError("WeCom credentials are unavailable")
            auth = await get_wecom_access_token(app_id, app_secret, strict=True)
            token = json_as_str_or(auth.get("access_token"))
            if not token:
                raise ValueError("WeCom access token is unavailable")
            response = await client.post(
                "https://qyapi.weixin.qq.com/cgi-bin/message/send",
                params={"access_token": token},
                json={
                    "touser": target.destination,
                    "msgtype": "text",
                    "agentid": target.enterprise_agent_id,
                    "text": {"content": chunk.text},
                },
            )
            body = response_body(response, target.provider)
            accepted = body.get("errcode") == 0 and not any(
                body.get(key) for key in ("invaliduser", "invalidparty", "invalidtag", "unlicenseduser")
            )
            require_ack(response, target.provider, accepted, str(body.get("errcode", "missing_ack")))
        case OutboundProvider.DINGTALK:
            from app.services.dingtalk_service import get_dingtalk_access_token

            if not app_id or not app_secret:
                raise ValueError("DingTalk credentials are unavailable")
            auth = await get_dingtalk_access_token(app_id, app_secret, strict=True)
            token = json_as_str_or(auth.get("access_token"))
            if not token:
                raise ValueError("DingTalk access token is unavailable")
            response = await client.post(
                "https://api.dingtalk.com/v1.0/robot/oToMessages/batchSend",
                headers={"x-acs-dingtalk-access-token": token},
                json={
                    "robotCode": app_id,
                    "userIds": [target.destination],
                    "msgKey": "sampleText",
                    "msgParam": json.dumps({"content": chunk.text}),
                },
            )
            body = response_body(response, target.provider)
            accepted = bool(body.get("processQueryKey")) and not any(
                body.get(key) for key in ("code", "errcode", "invalidStaffIdList", "flowControlledStaffIdList")
            )
            require_ack(response, target.provider, accepted, "missing_ack")
        case OutboundProvider.WECHAT:
            from app.services.wechat_channel import (
                WECHAT_CHANNEL_VERSION,
                build_wechat_headers,
                get_wechat_context_entry,
            )

            extra = json_object_from(config.extra_config)
            context = json_object_from(get_wechat_context_entry(config.extra_config, from_user_id=target.destination))
            context_token = json_as_str_or(context.get("context_token"))
            if not app_id or not app_secret or not context_token:
                raise ValueError("WeChat credentials or destination context are unavailable")
            response = await client.post(
                f"{target.endpoint}/ilink/bot/sendmessage",
                headers=build_wechat_headers(app_secret, json_as_str_or(extra.get("route_tag")) or None),
                json={
                    "msg": {
                        "from_user_id": "",
                        "to_user_id": target.destination,
                        "client_id": str(uuid4()),
                        "message_type": 2,
                        "message_state": 2,
                        "context_token": context_token,
                        "item_list": [{"type": 1, "text_item": {"text": chunk.text}}],
                    },
                    "base_info": {"channel_version": WECHAT_CHANNEL_VERSION},
                },
            )
            body = response_body(response, target.provider)
            codes = [body[key] for key in ("ret", "errcode") if key in body]
            require_ack(response, target.provider, bool(codes) and all(code == 0 for code in codes), "missing_ack")
        case _:
            raise ValueError("Unsupported enterprise transport")
