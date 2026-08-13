"""Trigger invocation and delivery orchestration."""

from __future__ import annotations

import json as _json
import uuid
from datetime import UTC, datetime
from typing import Any, TypedDict

from app.core.logging import logger
from app.dao.agent_dao import agent_dao
from app.dao.chat_dao import chat_message_dao, chat_session_dao
from app.dao.llm_dao import llm_model_dao
from app.dao.participant_dao import participant_dao
from app.db.session import connection_ctx
from app.services.llm.turn import TurnContext
from app.services.llm.types import OpenAIMessage
from app.services.trigger_runtime import (
    mark_trigger_executions_completed,
    mark_trigger_executions_failed,
)


class TriggerDeliveryTarget(TypedDict):
    kind: str
    session_id: str
    owner_user_id: str
    source_channel: str


async def resolve_trigger_delivery_target(agent: Any, triggers: list[Any]) -> TriggerDeliveryTarget | None:
    from app.services.chat_session_service import ensure_primary_platform_session

    for trigger in triggers:
        cfg = trigger.config or {}
        a2a_sid = cfg.get("_a2a_session_id")
        if a2a_sid:
            if not isinstance(a2a_sid, str):
                return None
            try:
                session = await chat_session_dao.get(uuid.UUID(a2a_sid))
                if not session:
                    return None
                return {
                    "kind": "session",
                    "session_id": str(session.id),
                    "owner_user_id": str(session.user_id),
                    "source_channel": session.source_channel,
                }
            except Exception:
                return None

    origin_cfg = None
    for trigger in triggers:
        cfg = trigger.config or {}
        if cfg.get("_origin_session_id") or cfg.get("_origin_user_id"):
            origin_cfg = cfg
            break
    if not origin_cfg:
        return None

    origin_source_channel = origin_cfg.get("_origin_source_channel")
    origin_session_id = origin_cfg.get("_origin_session_id")
    origin_user_id = origin_cfg.get("_origin_user_id")

    if origin_source_channel == "agent" and origin_session_id:
        if not isinstance(origin_session_id, str):
            return None
        try:
            session = await chat_session_dao.get(uuid.UUID(origin_session_id))
            if not session:
                return None
            return {
                "kind": "session",
                "session_id": str(session.id),
                "owner_user_id": str(session.user_id),
                "source_channel": "agent",
            }
        except Exception:
            return None

    if origin_source_channel != "trigger" and origin_user_id:
        if not isinstance(origin_user_id, str):
            return None
        try:
            primary = await ensure_primary_platform_session(None, agent.id, uuid.UUID(origin_user_id))
            return {
                "kind": "primary_user_session",
                "session_id": str(primary.id),
                "owner_user_id": str(primary.user_id),
                "source_channel": primary.source_channel,
            }
        except Exception:
            return None

    return None


async def invoke_agent_for_triggers(agent_id: uuid.UUID, triggers: list[Any]):
    from app.services.audit_logger import write_audit_log
    from app.services.llm import call_llm

    try:
        execution_ids = [
            uuid.UUID(str((t.config or {}).get("_execution_id")))
            for t in triggers
            if (t.config or {}).get("_execution_id")
        ]
        agent = await agent_dao.get(agent_id)
        if not agent or agent.is_expired:
            if execution_ids:
                await mark_trigger_executions_failed(execution_ids, "Agent not found or is expired")
            return

        if not agent.primary_model_id:
            logger.warning(f"Agent {agent.name} has no LLM model, skipping trigger invocation")
            if execution_ids:
                await mark_trigger_executions_failed(execution_ids, "Agent has no LLM model configured")
            return
        model = await llm_model_dao.get(agent.primary_model_id)
        if not model or not model.enabled:
            logger.warning(f"Agent {agent.name}'s model is unavailable, skipping trigger invocation")
            if execution_ids:
                await mark_trigger_executions_failed(execution_ids, "Agent primary model is unavailable or disabled")
            return

        context_parts = []
        trigger_names = []
        for t in triggers:
            part = f"Trigger: {t.name} ({t.type})\nReason: {t.reason}"
            if t.name == "daily_okr_collection":
                part += (
                    "\nExecution requirements: First call get_okr_settings to confirm whether daily report collection is enabled. "
                    "If enabled, contact only members and digital employees in your relationship network to collect today's final daily reports, "
                    "then organize them into a formal report of no more than 2,000 words. "
                    "If it is disabled, state that no action is needed and stop."
                )
            elif t.name in ("daily_okr_report", "weekly_okr_report", "monthly_okr_report"):
                part += (
                    "\nExecution requirements: The system automatically generates this company-wide report. "
                    "If you are awakened, provide only necessary clarification and do not collect reports from members again."
                )
            elif t.name == "biweekly_okr_checkin":
                part += (
                    "\nExecution requirements: First call get_okr_settings to confirm whether OKRs are enabled. "
                    "If enabled, check the company and member OKRs for the current period and proactively remind members who have not set OKRs or whose progress is behind. "
                    "If they are disabled, state that no action is needed and stop."
                )
            if t.focus_ref:
                part += f"\nRelated Focus: {t.focus_ref}"
            cfg = t.config or {}
            matched_message = cfg.get("_matched_message")
            if t.type == "on_message" and matched_message:
                if not isinstance(matched_message, str):
                    raise TypeError("Trigger matched message must be a string")
                part += f'\nMessage received from {cfg.get("_matched_from", "?")}:\n"{matched_message[:500]}"'
            if t.type == "on_message" and cfg.get("okr_member_id") and cfg.get("okr_report_date"):
                part += (
                    "\nExecution requirements: This event stores a daily-report reply."
                    f"\n1. Organize the response into a final daily report of no more than 2,000 words."
                    f'\n2. Immediately call upsert_member_daily_report(report_date="{cfg["okr_report_date"]}", '
                    f'member_type="{cfg.get("okr_member_type", "user")}", '
                    f'member_id="{cfg["okr_member_id"]}", content="<organized daily report>").'
                    "\n3. After the tool call succeeds, send a brief confirmation that clearly says the response was received and recorded."
                    "\n4. Do not only acknowledge receipt without calling the tool, and do not store the raw long conversation unchanged as the daily report."
                )
            webhook_payload = cfg.get("_webhook_payload")
            if t.type == "webhook" and webhook_payload:
                if not isinstance(webhook_payload, str):
                    raise TypeError("Trigger webhook payload must be a string")
                payload_str = webhook_payload
                if len(payload_str) > 2000:
                    payload_str = payload_str[:2000] + "... (truncated)"
                part += f"\nWebhook Payload:\n{payload_str}"
            context_parts.append(part)
            trigger_names.append(t.name)

        trigger_context = (
            "===== Current Wake Context =====\n"
            f"Wake source: trigger ({'multiple triggers fired simultaneously' if len(triggers) > 1 else 'trigger fired'})\n\n"
            + "\n---\n".join(context_parts)
            + "\n==========================="
        )

        title = f"🤖 Inner Monologue: {', '.join(trigger_names)}"
        agent_participant = await participant_dao.get_by_type_ref("agent", agent_id)
        agent_participant_id = agent_participant.id if agent_participant else None

        session = await chat_session_dao.create(
            obj_in={
                "agent_id": agent_id,
                "user_id": agent.creator_id,
                "participant_id": agent_participant_id,
                "source_channel": "trigger",
                "title": title[:200],
            }
        )
        session_id = session.id
        trigger_message: OpenAIMessage = {"role": "user", "content": trigger_context}
        messages = [trigger_message]
        await chat_message_dao.insert_message(
            agent_id=agent_id,
            user_id=agent.creator_id,
            role="user",
            content=trigger_context,
            conversation_id=str(session_id),
            participant_id=agent_participant_id,
        )

        collected_content: list[str] = []
        delivered_platform_message_via_tool = False

        async def on_chunk(text):
            collected_content.append(text)

        async def on_tool_call(data):
            nonlocal delivered_platform_message_via_tool
            try:
                tool_name = data.get("name")
                tool_status = data.get("status")
                if tool_status == "done" and tool_name == "send_platform_message":
                    result_text = str(data.get("result", ""))
                    if result_text.startswith("✅"):
                        delivered_platform_message_via_tool = True

                if data["status"] == "running":
                    await chat_message_dao.insert_message(
                        agent_id=agent_id,
                        user_id=agent.creator_id,
                        role="tool_call",
                        content=_json.dumps(
                            {"name": data["name"], "args": data["args"]}, ensure_ascii=False, default=str
                        ),
                        conversation_id=str(session_id),
                        participant_id=agent_participant_id,
                    )
                elif data["status"] == "done":
                    result_str = str(data.get("result", ""))[:2000]
                    await chat_message_dao.insert_message(
                        agent_id=agent_id,
                        user_id=agent.creator_id,
                        role="tool_call",
                        content=_json.dumps(
                            {"name": data["name"], "result": result_str}, ensure_ascii=False, default=str
                        ),
                        conversation_id=str(session_id),
                        participant_id=agent_participant_id,
                    )
            except Exception as e:
                logger.warning(f"Failed to persist tool call for trigger session: {e}")

        from_agent_name: str | None = None
        for t in triggers:
            cfg = t.config or {}
            candidate_name = cfg.get("from_agent_name")
            if isinstance(candidate_name, str) and candidate_name:
                from_agent_name = candidate_name
                break

        reply = await call_llm(
            model=model,
            messages=messages,
            agent_name=agent.name,
            role_description=agent.role_description or "",
            agent_id=agent_id,
            user_id=agent.creator_id,
            session_id=str(session_id),
            on_chunk=on_chunk,
            on_tool_call=on_tool_call,
            current_user_name_override=from_agent_name,
            turn=TurnContext(agent=agent, primary_model=model),
        )

        final_reply = reply or "".join(collected_content)
        is_a2a_internal = all(t.name == "a2a_wake" for t in triggers)
        notify_owner_user_id: str | None = None
        notify_session_id: str | None = None
        notification: str | None = None

        async with connection_ctx():
            agent_participant = await participant_dao.get_by_type_ref("agent", agent_id)
            await chat_message_dao.insert_message(
                agent_id=agent_id,
                user_id=agent.creator_id,
                role="assistant",
                content=final_reply,
                conversation_id=str(session_id),
                participant_id=agent_participant.id if agent_participant else None,
            )

        for t in triggers:
            a2a_sid = (t.config or {}).get("_a2a_session_id")
            if not (a2a_sid and final_reply):
                continue
            if not isinstance(a2a_sid, str):
                logger.warning("[A2A] Trigger A2A session ID is not a string; skipping persist")
                break
            try:
                async with connection_ctx():
                    participant = await participant_dao.get_by_type_ref("agent", agent_id)
                    await chat_message_dao.insert_message(
                        agent_id=agent_id,
                        user_id=agent.creator_id,
                        role="assistant",
                        content=final_reply,
                        conversation_id=a2a_sid,
                        participant_id=participant.id if participant else None,
                    )
                    chat_session = await chat_session_dao.get(uuid.UUID(a2a_sid))
                    if chat_session:
                        await chat_session_dao.update(
                            db_obj=chat_session,
                            obj_in={"last_message_at": datetime.now(UTC)},
                        )
            except Exception as e:
                logger.warning(f"[A2A] Failed to save reply to A2A session {a2a_sid}: {e}")
            break

        delivery_target = None if is_a2a_internal else await resolve_trigger_delivery_target(agent, triggers)

        if final_reply and delivery_target and not delivered_platform_message_via_tool:
            try:
                trigger_reasons = []
                for t in triggers:
                    summary_value = (t.config or {}).get("_notification_summary", "")
                    if not isinstance(summary_value, str):
                        raise TypeError("Trigger notification summary must be a string")
                    ns = summary_value.strip()
                    if ns:
                        trigger_reasons.append(ns)
                    else:
                        r = (t.reason or "").strip()
                        if r and len(r) <= 80:
                            trigger_reasons.append(r)
                        elif r:
                            trigger_reasons.append(r[:77] + "...")
                summary = trigger_reasons[0] if trigger_reasons else "There is a new event to process"
                notification = f"⚡ {summary}\n\n{final_reply}"
                notify_session_id = delivery_target["session_id"]
                notify_owner_user_id = delivery_target.get("owner_user_id")

                async with connection_ctx():
                    await chat_message_dao.insert_message(
                        agent_id=agent_id,
                        user_id=agent.creator_id,
                        role="assistant",
                        content=notification,
                        conversation_id=notify_session_id,
                    )
                    session_row = await chat_session_dao.get(uuid.UUID(notify_session_id))
                    if session_row:
                        await chat_session_dao.update(
                            db_obj=session_row,
                            obj_in={"last_message_at": datetime.now(UTC)},
                        )
                    if notify_owner_user_id:
                        from app.api.websocket import maybe_mark_session_read_for_active_viewer

                        await maybe_mark_session_read_for_active_viewer(
                            None,
                            agent_id=agent_id,
                            session_id=notify_session_id,
                            user_id=uuid.UUID(notify_owner_user_id),
                        )
            except Exception as e:
                logger.error(f"Failed to persist trigger delivery: {e}")
                notification = None
                notify_session_id = None
                notify_owner_user_id = None

        if notification and notify_session_id and notify_owner_user_id:
            try:
                from app.api.websocket import manager as ws_manager

                await ws_manager.send_to_user(
                    str(agent_id),
                    notify_owner_user_id,
                    {
                        "type": "trigger_notification",
                        "content": notification,
                        "triggers": [t.name for t in triggers],
                        "session_id": notify_session_id,
                    },
                )
            except Exception as e:
                logger.error(f"Failed to push trigger result to WebSocket: {e}")

        await write_audit_log(
            "trigger_fired",
            {"agent_name": agent.name, "triggers": [{"name": t.name, "type": t.type} for t in triggers]},
            agent_id=agent_id,
        )

        if execution_ids:
            await mark_trigger_executions_completed(execution_ids)
    except Exception as e:
        logger.error(f"Failed to invoke agent {agent_id} for triggers: {e}")
        import traceback

        traceback.print_exc()
        execution_ids = [
            uuid.UUID(str((t.config or {}).get("_execution_id")))
            for t in triggers
            if (t.config or {}).get("_execution_id")
        ]
        if execution_ids:
            await mark_trigger_executions_failed(execution_ids, str(e)[:2000])
