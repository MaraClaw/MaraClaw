"""Gateway API for OpenClaw agent communication.

OpenClaw agents authenticate via X-Api-Key header and use these endpoints
to report results, send messages, and send heartbeat pings.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Header, HTTPException

from app.core.acl_locks import lock_agent, lock_tenants, lock_user
from app.core.domain.channels import ChannelType
from app.core.logging import logger
from app.core.permissions import evaluate_agent_relationship_status, evaluate_human_relationship_status
from app.dao.agent_agent_relationship_dao import agent_agent_relationship_dao
from app.dao.agent_dao import agent_dao
from app.dao.agent_relationship_dao import agent_relationship_dao
from app.dao.channel_config_dao import channel_config_dao
from app.dao.gateway_message_dao import gateway_message_dao
from app.db.session import connection_ctx
from app.records.agent import AgentRecord
from app.schemas.channel_response_contracts import (
    GatewayHeartbeatResponse,
    GatewaySendMessageResponse,
    StatusAcknowledgement,
)
from app.schemas.schemas import (
    GatewayReportRequest,
    GatewaySendMessageRequest,
)
from app.services.openclaw_gateway_delivery import publish_report, record_report
from app.services.openclaw_routing import enqueue_openclaw_message

router = APIRouter(prefix="/gateway", tags=["gateway"])


async def _get_agent_by_key(api_key: str) -> AgentRecord:
    """Authenticate an OpenClaw agent by its API key."""
    agent = await agent_dao.get_openclaw_by_api_key(api_key)
    if not agent:
        raise HTTPException(status_code=401, detail="Invalid API key")
    return agent


# ─── Report results ─────────────────────────────────────


@router.post("/report", response_model=None)
async def report_result(
    body: GatewayReportRequest, x_api_key: str = Header(None, alias="X-Api-Key")
) -> StatusAcknowledgement:
    """OpenClaw agent reports the result of a processed message."""
    if not x_api_key:
        raise HTTPException(status_code=401, detail="Missing X-Api-Key header")
    agent = await _get_agent_by_key(x_api_key)
    logger.info("[Gateway] report called agent={} msg_id={}", agent.id, body.message_id)

    async with connection_ctx() as db:
        await lock_tenants(db, (agent.tenant_id,))
        await lock_user(db, agent.creator_id, agent.tenant_id)
        await lock_agent(db, agent.id, agent.tenant_id)
        active = await agent_dao.lock_openclaw(agent)
        if active is None or (active.creator_id, active.tenant_id) != (agent.creator_id, agent.tenant_id):
            raise HTTPException(status_code=401, detail="Invalid API key")
        agent = active
        won = await record_report(agent, body.message_id, body.result)
    if won is None:
        if await gateway_message_dao.get_for_agent(body.message_id, agent.id) is None:
            raise HTTPException(status_code=404, detail="Message not found")
        logger.info("[Gateway] Ignored duplicate report message={} agent={}", body.message_id, agent.id)
        return {"status": "ok"}
    await publish_report(agent, won)
    return {"status": "ok"}


# ─── Heartbeat ──────────────────────────────────────────


@router.post("/heartbeat", response_model=None)
async def heartbeat(x_api_key: str = Header(..., alias="X-Api-Key")) -> GatewayHeartbeatResponse:
    """Pure heartbeat ping - keeps the OpenClaw agent marked as online."""
    agent = await _get_agent_by_key(x_api_key)
    if await agent_dao.touch_openclaw(agent) is None:
        raise HTTPException(status_code=401, detail="Invalid API key")
    return {"status": "ok", "agent_id": str(agent.id)}


# ─── Send message ───────────────────────────────────────


@router.post("/send-message", response_model=None)
async def send_message(
    body: GatewaySendMessageRequest, x_api_key: str = Header(..., alias="X-Api-Key")
) -> GatewaySendMessageResponse:
    """OpenClaw agent sends a message to a person or another agent.

    Routes automatically based on target type:
    - Agent target: enqueue for the peer guest; native execution settles its reply
    - Human target: send via available channel (feishu, etc.)
    """
    agent = await _get_agent_by_key(x_api_key)
    target_name = body.target.strip()
    content = body.content.strip()
    channel_hint = (body.channel or "").strip().lower()

    async with connection_ctx():
        active = await agent_dao.lock_openclaw(agent)
        if active is None:
            raise HTTPException(status_code=401, detail="Invalid API key")
        agent = active
        _ = await agent_dao.update(db_obj=agent, obj_in={"openclaw_last_seen": datetime.now(UTC)})

        # 1. Try to find target as another Agent, limited to active relationships.
        target_agent = None
        for rel in await agent_agent_relationship_dao.list_for_agent_with_targets(agent.id):
            candidate = rel.target_agent
            if not candidate:
                continue
            status_info = await evaluate_agent_relationship_status(rel)
            if status_info["access_status"] != "active":
                continue
            if candidate.name.lower() == target_name.lower() or target_name.lower() in candidate.name.lower():
                target_agent = candidate
                break

        logger.info(
            f"[Gateway] send_message: target='{target_name}', found_agent={target_agent.name if target_agent else None}, agent_type={getattr(target_agent, 'agent_type', None) if target_agent else None}, channel_hint='{channel_hint}'"
        )

        if target_agent and (not channel_hint or channel_hint == "agent"):
            conv_id = f"gw_agent_{agent.id}_{target_agent.id}"

            _ = await enqueue_openclaw_message(
                agent=target_agent,
                content=content,
                sender_agent_id=agent.id,
                conversation_id=conv_id,
            )
            return {
                "status": "accepted",
                "target": target_agent.name,
                "type": "openclaw_agent",
                "message": f"Message sent to {target_agent.name}.",
            }

        # 2. Try to find target as a human (via relationships)
        rels = await agent_relationship_dao.list_for_agent_with_members(agent.id)

        target_member = None
        for r in rels:
            status_info = await evaluate_human_relationship_status(r, source_agent=agent)
            if r.member and status_info["access_status"] == "active" and r.member.name == target_name:
                target_member = r.member
                break
        # Fuzzy match if exact match fails
        if not target_member:
            for r in rels:
                status_info = await evaluate_human_relationship_status(r, source_agent=agent)
                if (
                    r.member
                    and status_info["access_status"] == "active"
                    and target_name.lower() in r.member.name.lower()
                ):
                    target_member = r.member
                    break

    if not target_member:
        raise HTTPException(status_code=404, detail=f"Target '{target_name}' not found. Check your relationships list.")

    # Send via feishu if available
    if (target_member.external_id or target_member.open_id) not in (None, "") and (
        channel_hint in (None, "") or channel_hint == "feishu"
    ):
        import json as _json

        from app.services.feishu_service import feishu_service

        config = await channel_config_dao.get_for_agent(agent_id=agent.id, channel_type=ChannelType.FEISHU)
        if not config and agent.tenant_id:
            # Try to find any feishu config in the org
            config = await channel_config_dao.get_for_tenant_channel(
                tenant_id=agent.tenant_id,
                channel_type=ChannelType.FEISHU,
            )

        if not config:
            raise HTTPException(status_code=400, detail="No Feishu channel configured")

        # Extract config values before Feishu HTTP calls
        _cfg_app_id = config.app_id
        _cfg_app_secret = config.app_secret
        if (
            not isinstance(_cfg_app_id, str)
            or not _cfg_app_id
            or not isinstance(_cfg_app_secret, str)
            or not _cfg_app_secret
        ):
            raise HTTPException(status_code=400, detail="No Feishu channel configured")

        # Prefer user_id (tenant-stable, works across apps), fallback to open_id
        resp = None
        if target_member.external_id not in (None, ""):
            resp = await feishu_service.send_message(
                _cfg_app_id,
                _cfg_app_secret,
                receive_id=target_member.external_id,
                msg_type="text",
                content=_json.dumps({"text": content}, ensure_ascii=False),
                receive_id_type="user_id",
            )
        if (resp is None or resp.get("code") != 0) and target_member.open_id not in (None, ""):
            resp = await feishu_service.send_message(
                _cfg_app_id,
                _cfg_app_secret,
                receive_id=target_member.open_id,
                msg_type="text",
                content=_json.dumps({"text": content}, ensure_ascii=False),
                receive_id_type="open_id",
            )

        if resp is not None and resp and resp.get("code") == 0:
            return {
                "status": "sent",
                "target": target_member.name,
                "type": "human",
                "channel": "feishu",
            }
        raise HTTPException(
            status_code=502,
            detail=f"Feishu send failed: {resp.get('msg') if resp is not None and resp else 'no ID available'} (code {resp.get('code') if resp is not None and resp else 'N/A'})",
        )

    raise HTTPException(
        status_code=400,
        detail=f"No available channel to reach {target_member.name}. feishu_user_id={'yes' if target_member.external_id not in (None, '') else 'no'}, feishu_open_id={'yes' if target_member.open_id not in (None, '') else 'no'}",
    )
