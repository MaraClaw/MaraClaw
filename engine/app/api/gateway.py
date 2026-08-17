"""Gateway API for OpenClaw agent communication.

OpenClaw agents authenticate via X-Api-Key header and use these endpoints
to poll for messages, report results, send messages, and send heartbeat pings.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Header, HTTPException

from app.core.logging import logger
from app.core.permissions import evaluate_agent_relationship_status, evaluate_human_relationship_status
from app.dao.agent_agent_relationship_dao import agent_agent_relationship_dao
from app.dao.agent_dao import agent_dao
from app.dao.agent_relationship_dao import agent_relationship_dao
from app.dao.channel_config_dao import channel_config_dao
from app.dao.chat_dao import chat_message_dao, chat_session_dao
from app.dao.gateway_message_dao import gateway_message_dao
from app.dao.participant_dao import participant_dao
from app.dao.user_dao import user_dao
from app.records.agent import AgentRecord
from app.schemas.schemas import (
    GatewayHistoryItem,
    GatewayMessageOut,
    GatewayPollResponse,
    GatewayRelationshipItem,
    GatewayReportRequest,
    GatewaySendMessageRequest,
)
from app.services.openclaw_routing import enqueue_openclaw_message, poll_model_hint

router = APIRouter(prefix="/gateway", tags=["gateway"])


async def _get_agent_by_key(api_key: str, db: object | None = None) -> AgentRecord:
    """Authenticate an OpenClaw agent by its API key."""
    del db
    from app.services.openclaw_hot_cache import get_cached_agent_by_key, set_cached_agent_by_key

    cached = get_cached_agent_by_key(api_key)
    if cached is not None:
        return cached
    agent = await agent_dao.get_openclaw_by_api_key(api_key)
    if not agent:
        raise HTTPException(status_code=401, detail="Invalid API key")
    set_cached_agent_by_key(api_key, agent)
    return agent


# ─── Poll for messages ──────────────────────────────────


@router.get("/poll", response_model=GatewayPollResponse)
async def poll_messages(x_api_key: str = Header(..., alias="X-Api-Key"), db: object | None = None):
    """OpenClaw agent polls for pending messages.

    Returns all pending messages and marks them as delivered.
    Also updates openclaw_last_seen for online status tracking.
    """
    logger.debug("[Gateway] poll called, key_prefix={}...", x_api_key[:8])
    agent = await _get_agent_by_key(x_api_key, db)

    from app.services.openclaw_hot_cache import should_touch_last_seen

    if should_touch_last_seen(agent.id):
        agent = (
            await agent_dao.update(
                db_obj=agent,
                obj_in={"openclaw_last_seen": datetime.now(UTC), "status": "running"},
            )
            or agent
        )

    # Fetch pending messages
    messages = await gateway_message_dao.list_pending(agent.id)
    guest_model, guest_slot = poll_model_hint(messages)

    # Mark as delivered
    now = datetime.now(UTC)
    out = []
    for msg in messages:
        _ = await gateway_message_dao.update(
            db_obj=msg,
            obj_in={"status": "delivered", "delivered_at": now},
        )

        # Resolve sender names
        sender_agent_name = None
        sender_user_name = None
        if msg.sender_agent_id:
            sender_agent = await agent_dao.get(msg.sender_agent_id)
            sender_agent_name = sender_agent.name if sender_agent else None
        if msg.sender_user_id:
            sender_user_name = await user_dao.display_name_for_id(msg.sender_user_id)

        # Fetch conversation history (last 10 messages) for context
        history = []
        if msg.conversation_id:
            hist_msgs = await chat_message_dao.list_for_session(
                conversation_id=msg.conversation_id,
                limit=10,
            )
            for h in hist_msgs:
                # Resolve sender name for each history message
                h_sender = None
                if h.role == "user" and h.user_id:
                    h_sender = await user_dao.display_name_for_id(h.user_id)
                elif h.role == "assistant":
                    h_sender = agent.name
                history.append(
                    GatewayHistoryItem(
                        role=h.role,
                        content=h.content or "",
                        sender_name=h_sender,
                        created_at=h.created_at,
                    )
                )

        out.append(
            GatewayMessageOut(
                id=msg.id,
                conversation_id=msg.conversation_id,
                sender_agent_name=sender_agent_name,
                sender_user_name=sender_user_name,
                sender_user_id=str(msg.sender_user_id) if msg.sender_user_id else None,
                content=msg.content,
                created_at=msg.created_at,
                history=history,
                model=guest_model,
                model_slot=guest_slot,
            )
        )

    # Inbox turns should answer first; relationship catalogs wait for empty polls.
    rel_items = [] if out else await _poll_relationships(agent)
    return GatewayPollResponse(messages=out, relationships=rel_items)


async def _poll_relationships(agent: AgentRecord) -> list[GatewayRelationshipItem]:
    rel_items: list[GatewayRelationshipItem] = []
    for r in await agent_relationship_dao.list_for_agent_with_members(agent.id):
        status_info = await evaluate_human_relationship_status(None, r, source_agent=agent)
        if r.member and status_info["access_status"] == "active":
            channels = []
            if getattr(r.member, "external_id", None) or getattr(r.member, "open_id", None):
                channels.append("feishu")
            if getattr(r.member, "email", None):
                channels.append("email")
            rel_items.append(
                GatewayRelationshipItem(
                    name=r.member.name,
                    type="human",
                    role=r.relation,
                    description=r.description or None,
                    channels=channels,
                )
            )
    for r in await agent_agent_relationship_dao.list_for_agent_with_targets(agent.id):
        status_info = await evaluate_agent_relationship_status(None, r)
        if r.target_agent and status_info["access_status"] == "active":
            rel_items.append(
                GatewayRelationshipItem(
                    name=r.target_agent.name,
                    type="agent",
                    role=r.relation,
                    description=r.description or None,
                    channels=["agent"],
                )
            )
    return rel_items


# ─── Report results ─────────────────────────────────────


@router.post("/report")
async def report_result(
    body: GatewayReportRequest, x_api_key: str = Header(None, alias="X-Api-Key"), db: object | None = None
):
    """OpenClaw agent reports the result of a processed message."""
    if not x_api_key:
        raise HTTPException(status_code=401, detail="Missing X-Api-Key header")
    logger.info(f"[Gateway] report called, key_prefix={x_api_key[:8]}..., msg_id={body.message_id}")
    agent = await _get_agent_by_key(x_api_key, db)

    msg = await gateway_message_dao.get_for_agent(body.message_id, agent.id)
    if not msg:
        raise HTTPException(status_code=404, detail="Message not found")

    _ = await gateway_message_dao.update(
        db_obj=msg,
        obj_in={
            "status": "completed",
            "result": body.result,
            "completed_at": datetime.now(UTC),
        },
    )

    # Update last seen
    _ = await agent_dao.update(db_obj=agent, obj_in={"openclaw_last_seen": datetime.now(UTC)})

    # Save result as assistant chat message and push via WebSocket
    # (works for both user-originated and agent-to-agent messages)
    session = None
    if msg.conversation_id:
        if body.result:
            participant = await participant_dao.get_by_type_ref("agent", agent.id)

            _ = await chat_message_dao.insert_message(
                agent_id=agent.id,
                user_id=msg.sender_user_id or getattr(agent, "creator_id", agent.id),
                role="assistant",
                content=body.result,
                conversation_id=msg.conversation_id,
                participant_id=participant.id if participant else None,
            )
            try:
                session = await chat_session_dao.get(uuid.UUID(msg.conversation_id))
            except ValueError, TypeError:
                session = None
        try:
            from app.api.websocket import manager

            await manager.send_to_session(
                str(agent.id),
                msg.conversation_id,
                {
                    "type": "done",
                    "role": "assistant",
                    "content": body.result or "",
                },
                user_id=str(msg.sender_user_id) if msg.sender_user_id else None,
            )
            logger.info(
                "[Gateway] pushed reply to session {} agent={}",
                msg.conversation_id,
                agent.id,
            )
        except Exception as error:
            logger.warning("[Gateway] Skipped done notification for disconnected user: {}", error)

    if body.result and session is not None:
        from app.services.channels.outbound import deliver_session_reply

        await deliver_session_reply(agent=agent, session=session, content=body.result)

    # If the original message was from another agent (OpenClaw-to-OpenClaw),
    # write the reply back as a gateway_message for the sender agent to poll
    if body.result and msg.sender_agent_id:
        conv_id = msg.conversation_id or f"gw_agent_{msg.sender_agent_id}_{agent.id}"
        sender = await agent_dao.get(msg.sender_agent_id)
        if sender:
            _ = await enqueue_openclaw_message(
                agent=sender,
                content=body.result,
                sender_agent_id=agent.id,
                conversation_id=conv_id,
                await_wake=False,
            )
        logger.info(f"[Gateway] Reply routed back to sender agent {msg.sender_agent_id}")

    return {"status": "ok"}


# ─── Heartbeat ──────────────────────────────────────────


@router.post("/heartbeat")
async def heartbeat(x_api_key: str = Header(..., alias="X-Api-Key"), db: object | None = None):
    """Pure heartbeat ping - keeps the OpenClaw agent marked as online."""
    agent = await _get_agent_by_key(x_api_key, db)
    _ = await agent_dao.update(
        db_obj=agent,
        obj_in={"openclaw_last_seen": datetime.now(UTC), "status": "running"},
    )
    return {"status": "ok", "agent_id": str(agent.id)}


# ─── Send message ───────────────────────────────────────


@router.post("/send-message")
async def send_message(
    body: GatewaySendMessageRequest, x_api_key: str = Header(..., alias="X-Api-Key"), db: object | None = None
) -> dict[str, Any]:
    """OpenClaw agent sends a message to a person or another agent.

    Routes automatically based on target type:
    - Agent target: enqueue for the peer guest; reply arrives on the next poll
    - Human target: send via available channel (feishu, etc.)
    """
    agent = await _get_agent_by_key(x_api_key, db)
    _ = await agent_dao.update(db_obj=agent, obj_in={"openclaw_last_seen": datetime.now(UTC)})

    target_name = body.target.strip()
    content = body.content.strip()
    channel_hint = (body.channel or "").strip().lower()

    # 1. Try to find target as another Agent, limited to active relationships.
    target_agent = None
    for rel in await agent_agent_relationship_dao.list_for_agent_with_targets(agent.id):
        candidate = rel.target_agent
        if not candidate:
            continue
        status_info = await evaluate_agent_relationship_status(None, rel)
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
            await_wake=False,
        )
        return {
            "status": "accepted",
            "target": target_agent.name,
            "type": "openclaw_agent",
            "message": f"Message sent to {target_agent.name}. Reply will appear in your next poll.",
        }

    # 2. Try to find target as a human (via relationships)
    rels = await agent_relationship_dao.list_for_agent_with_members(agent.id)

    target_member = None
    for r in rels:
        status_info = await evaluate_human_relationship_status(None, r, source_agent=agent)
        if r.member and status_info["access_status"] == "active" and r.member.name == target_name:
            target_member = r.member
            break
    # Fuzzy match if exact match fails
    if not target_member:
        for r in rels:
            status_info = await evaluate_human_relationship_status(None, r, source_agent=agent)
            if r.member and status_info["access_status"] == "active" and target_name.lower() in r.member.name.lower():
                target_member = r.member
                break

    if not target_member:
        raise HTTPException(status_code=404, detail=f"Target '{target_name}' not found. Check your relationships list.")

    # Send via feishu if available
    if (target_member.external_id or target_member.open_id) and (not channel_hint or channel_hint == "feishu"):
        import json as _json

        from app.services.feishu_service import feishu_service

        config = await channel_config_dao.get_for_agent(agent_id=agent.id, channel_type="feishu")
        if not config and agent.tenant_id:
            # Try to find any feishu config in the org
            config = await channel_config_dao.get_for_tenant_channel(
                tenant_id=agent.tenant_id,
                channel_type="feishu",
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
        if target_member.external_id:
            resp = await feishu_service.send_message(
                _cfg_app_id,
                _cfg_app_secret,
                receive_id=target_member.external_id,
                msg_type="text",
                content=_json.dumps({"text": content}, ensure_ascii=False),
                receive_id_type="user_id",
            )
        if (resp is None or resp.get("code") != 0) and target_member.open_id:
            resp = await feishu_service.send_message(
                _cfg_app_id,
                _cfg_app_secret,
                receive_id=target_member.open_id,
                msg_type="text",
                content=_json.dumps({"text": content}, ensure_ascii=False),
                receive_id_type="open_id",
            )

        if resp and resp.get("code") == 0:
            return {
                "status": "sent",
                "target": target_member.name,
                "type": "human",
                "channel": "feishu",
            }
        raise HTTPException(
            status_code=502,
            detail=f"Feishu send failed: {resp.get('msg') if resp else 'no ID available'} (code {resp.get('code') if resp else 'N/A'})",
        )

    raise HTTPException(
        status_code=400,
        detail=f"No available channel to reach {target_member.name}. feishu_user_id={'yes' if target_member.external_id else 'no'}, feishu_open_id={'yes' if target_member.open_id else 'no'}",
    )


# ─── Setup guide ────────────────────────────────────────


@router.get("/setup-guide/{agent_id}")
async def get_setup_guide(
    agent_id: uuid.UUID, x_api_key: str = Header(..., alias="X-Api-Key"), db: object | None = None
):
    """Return the pre-filled Skill file and Heartbeat instruction for this agent."""
    agent = await _get_agent_by_key(x_api_key, db)
    if agent.id != agent_id:
        raise HTTPException(status_code=403, detail="Key does not match this agent")

    # Note: we use the raw key from the header since the agent already authenticated
    from app.services.openclaw_inbox import guest_engine_base_url

    base_url = guest_engine_base_url()

    skill_content = f"""---
name: maraclaw_sync
description: Sync with MaraClaw platform - check inbox, submit results, and send messages.
---

# MaraClaw Sync

## When to use
Check for new messages from the MaraClaw platform during every heartbeat cycle.
You can also proactively send messages to people and agents in your relationships.

## Instructions

### 1. Check inbox
Make an HTTP GET request:
- URL: {base_url}/api/gateway/poll
- Header: X-Api-Key: {x_api_key}

The response contains a `messages` array. Each message includes:
- `id` - unique message ID (use this for reporting)
- `content` - the message text
- `sender_user_name` - name of the MaraClaw user who sent it
- `sender_user_id` - unique ID of the sender
- `conversation_id` - the conversation this message belongs to
- `history` - array of previous messages in this conversation for context
- `model` - provider/model ref for this inbox batch (for example openai/gpt-5.4)
- `model_slot` - "primary" (complex) or "secondary" (manageable)

**IMPORTANT**: Process the entire inbox batch with `model`. If your current
session is on a different model, switch first (`/model <model>`). Do not pick
a cheaper model on your own. Mixed batches are already fail-closed to primary.

The response also contains a `relationships` array describing your colleagues:
- `name` - the person or agent name
- `type` - "human" or "agent"
- `role` - relationship type (e.g. collaborator, supervisor)
- `channels` - available communication channels (e.g. ["feishu"], ["agent"])

**IMPORTANT**: Use the `history` array to understand conversation context before replying.
Different `sender_user_name` values mean different people - address them accordingly.

### 2. Report results
For each completed message, make an HTTP POST request:
- URL: {base_url}/api/gateway/report
- Header: X-Api-Key: {x_api_key}
- Header: Content-Type: application/json
- Body: {{"message_id": "<id from the message>", "result": "<your response>"}}

### 3. Send a message to someone
To proactively contact a person or agent, make an HTTP POST request:
- URL: {base_url}/api/gateway/send-message
- Header: X-Api-Key: {x_api_key}
- Header: Content-Type: application/json
- Body: {{"target": "<name of person or agent>", "content": "<your message>"}}

The system auto-detects the best channel. For agents, the reply appears in your next poll.
For humans, the message is delivered via their available channel (e.g. Feishu).
"""

    heartbeat_line = "- Check MaraClaw inbox using the maraclaw_sync skill and process any pending messages"

    return {
        "skill_filename": "maraclaw_sync.md",
        "skill_content": skill_content,
        "heartbeat_addition": heartbeat_line,
    }
