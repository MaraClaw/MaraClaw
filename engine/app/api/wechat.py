"""WeChat iLink Bot channel API routes."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import TypedDict

import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import Response

from app.config import get_settings
from app.core.permissions import check_agent_access, is_agent_creator
from app.core.security import get_current_user
from app.dao.channel_config_dao import channel_config_dao
from app.records.user import UserRecord
from app.schemas.schemas import ChannelConfigOut
from app.services.wechat_channel import WECHAT_CHANNEL_VERSION, WECHAT_ILINK_BASE_URL, wechat_poll_manager

router = APIRouter(tags=["wechat"])
settings = get_settings()


class WeChatQRCodePayload(TypedDict, total=False):
    route_tag: str


def _role_enabled(*required: str) -> bool:
    raw = (settings.PROCESS_ROLE or "all").strip().lower()
    roles = {part.strip() for part in raw.split(",") if part.strip()} or {"all"}
    return "all" in roles or any(role in roles for role in required)


def _route_tag(data: WeChatQRCodePayload | None = None) -> str | None:
    value = str((data or {}).get("route_tag") or "").strip()
    return value or None


def _build_qrcode_headers(route_tag: str | None = None) -> dict[str, str]:
    headers: dict[str, str] = {}
    if route_tag:
        headers["SKRouteTag"] = route_tag
    return headers


def _validate_qrcode_proxy_url(url: str) -> str:
    value = url.strip()
    if not value.startswith(("https://liteapp.weixin.qq.com/", "https://weixin.qq.com/")):
        raise HTTPException(status_code=400, detail="Unsupported QR code image URL")
    return value


@router.post("/agents/{agent_id}/wechat-channel/qrcode")
async def create_wechat_qrcode(
    agent_id: uuid.UUID, data: WeChatQRCodePayload | None = None, current_user: UserRecord = Depends(get_current_user)
):
    agent, _ = await check_agent_access(current_user, agent_id)
    if not is_agent_creator(current_user, agent):
        raise HTTPException(status_code=403, detail="Only creator can configure channel")

    route_tag = _route_tag(data)
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.get(
            f"{WECHAT_ILINK_BASE_URL}/ilink/bot/get_bot_qrcode",
            params={"bot_type": 3},
            headers=_build_qrcode_headers(route_tag),
        )
        payload = resp.json()
        if resp.status_code >= 400:
            raise HTTPException(status_code=resp.status_code, detail=str(payload)[:300])
        return payload


@router.get("/agents/{agent_id}/wechat-channel/qrcode-status")
async def get_wechat_qrcode_status(
    agent_id: uuid.UUID, qrcode: str, route_tag: str | None = None, current_user: UserRecord = Depends(get_current_user)
):
    agent, _ = await check_agent_access(current_user, agent_id)
    if not is_agent_creator(current_user, agent):
        raise HTTPException(status_code=403, detail="Only creator can configure channel")

    async with httpx.AsyncClient(timeout=40) as client:
        resp = await client.get(
            f"{WECHAT_ILINK_BASE_URL}/ilink/bot/get_qrcode_status",
            params={"qrcode": qrcode},
            headers={
                "iLink-App-ClientVersion": "1",
                **_build_qrcode_headers(route_tag),
            },
        )
        payload = resp.json()
        if resp.status_code >= 400:
            raise HTTPException(status_code=resp.status_code, detail=str(payload)[:300])

    if payload.get("status") == "confirmed":
        existing = await channel_config_dao.get_for_agent(agent_id=agent_id, channel_type="wechat")
        extra = {
            "bot_token": payload.get("bot_token", ""),
            "ilink_user_id": payload.get("ilink_user_id", ""),
            "baseurl": payload.get("baseurl") or WECHAT_ILINK_BASE_URL,
            "get_updates_buf": "",
            "channel_version": WECHAT_CHANNEL_VERSION,
            "session_expired": False,
            "saved_at": datetime.now(UTC).isoformat(),
        }
        if route_tag:
            extra["route_tag"] = route_tag

        if existing:
            await channel_config_dao.update(
                db_obj=existing,
                obj_in={
                    "app_id": payload.get("ilink_bot_id", ""),
                    "app_secret": payload.get("bot_token", ""),
                    "extra_config": extra,
                    "is_configured": True,
                    "is_connected": False,
                },
            )
        else:
            await channel_config_dao.create(
                obj_in={
                    "agent_id": agent_id,
                    "channel_type": "wechat",
                    "app_id": payload.get("ilink_bot_id", ""),
                    "app_secret": payload.get("bot_token", ""),
                    "extra_config": extra,
                    "is_configured": True,
                    "is_connected": False,
                }
            )

        if _role_enabled("connector"):
            from app.api.background_tasks import schedule_background_task

            schedule_background_task(wechat_poll_manager.start_client(agent_id), "start WeChat polling client")

    return payload


@router.get("/agents/{agent_id}/wechat-channel/qrcode-image")
async def get_wechat_qrcode_image(agent_id: uuid.UUID, url: str, current_user: UserRecord = Depends(get_current_user)):
    agent, _ = await check_agent_access(current_user, agent_id)
    if not is_agent_creator(current_user, agent):
        raise HTTPException(status_code=403, detail="Only creator can configure channel")

    target_url = _validate_qrcode_proxy_url(url)
    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        resp = await client.get(target_url)
        if resp.status_code >= 400:
            raise HTTPException(status_code=resp.status_code, detail="Failed to fetch WeChat QR image")

    media_type = resp.headers.get("content-type", "image/png").split(";")[0].strip() or "image/png"
    return Response(content=resp.content, media_type=media_type)


@router.get("/agents/{agent_id}/wechat-channel", response_model=ChannelConfigOut)
async def get_wechat_channel(agent_id: uuid.UUID, current_user: UserRecord = Depends(get_current_user)):
    await check_agent_access(current_user, agent_id)
    config = await channel_config_dao.get_for_agent(agent_id=agent_id, channel_type="wechat")
    if not config:
        raise HTTPException(status_code=404, detail="WeChat not configured")
    return ChannelConfigOut.model_validate(config)


@router.delete("/agents/{agent_id}/wechat-channel", status_code=status.HTTP_204_NO_CONTENT)
async def delete_wechat_channel(agent_id: uuid.UUID, current_user: UserRecord = Depends(get_current_user)):
    agent, _ = await check_agent_access(current_user, agent_id)
    if not is_agent_creator(current_user, agent):
        raise HTTPException(status_code=403, detail="Only creator can remove channel")

    config = await channel_config_dao.get_for_agent(agent_id=agent_id, channel_type="wechat")
    if not config:
        raise HTTPException(status_code=404, detail="WeChat not configured")

    await wechat_poll_manager.stop_client(agent_id)
    await channel_config_dao.delete(id=config.id)
