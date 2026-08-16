from __future__ import annotations

from contextlib import suppress
from datetime import UTC, datetime

from app.core.logging import logger
from app.dao.chat_dao import chat_message_dao
from app.dao.gateway_message_dao import gateway_message_dao
from app.dao.participant_dao import participant_dao
from app.services import agent_tools

from .a2a_context import A2AContext


async def _a2a_handle_openclaw(ctx: A2AContext) -> str:
    try:
        _ = await gateway_message_dao.create(
            obj_in={
                "agent_id": ctx.target_agent.id,
                "sender_agent_id": ctx.source_agent.id,
                "sender_user_id": ctx.owner_id,
                "content": f"[From {ctx.source_agent.name}] {ctx.message_text}",
                "status": "pending",
                "conversation_id": ctx.chat_session_id,
            }
        )

        from app.services.activity_logger import log_activity

        await log_activity(
            ctx.source_agent.id,
            "agent_msg_sent",
            f"Sent message to {ctx.target_agent.name} (queued)",
            detail={"partner": ctx.target_agent.name, "message": ctx.message_text[:200]},
        )

        online = (
            ctx.target_agent.openclaw_last_seen
            and (datetime.now(UTC) - ctx.target_agent.openclaw_last_seen).total_seconds() < 300
        )
        status_hint = "online" if online else "offline (message will be delivered on next heartbeat)"
        return (
            f"✅ Message sent to {ctx.target_agent.name} (OpenClaw agent, currently {status_hint}). "
            + "The message has been queued and will be delivered when the agent polls for updates."
        )
    except Exception as error:
        logger.exception(f"[A2A] _a2a_handle_openclaw failed: from={ctx.source_agent.id}, to={ctx.target_agent.id}")
        return f"❌ OpenClaw send error ({type(error).__name__}): {str(error)[:200]}"


async def _a2a_handle_notify(ctx: A2AContext) -> str:
    try:
        with suppress(Exception):
            from app.services.activity_logger import log_activity

            await log_activity(
                ctx.source_agent.id,
                "agent_msg_sent",
                f"Sent notification to {ctx.target_agent.name}",
                detail={"partner": ctx.target_agent.name, "message": ctx.message_text[:200], "msg_type": "notify"},
            )

        try:
            await agent_tools._wake_agent_async(
                ctx.target_agent.id,
                f"[From {ctx.source_agent.name}] {ctx.message_text}",
                from_agent_id=ctx.source_agent.id,
                skip_dedup=True,
                a2a_session_id=ctx.chat_session_id,
            )
        except Exception as error:
            logger.warning(f"[A2A] Failed to wake {ctx.target_agent.name} for notify: {error}")

        return f"✅ Notification sent to {ctx.target_agent.name}. They will process it asynchronously."
    except Exception as error:
        logger.exception(f"[A2A] _a2a_handle_notify failed: from={ctx.source_agent.id}, to={ctx.target_agent.id}")
        return f"❌ Notification error ({type(error).__name__}): {str(error)[:200]}"


async def _a2a_handle_task_delegate(ctx: A2AContext) -> str:
    try:
        focus_id = f"wait_{ctx.target_agent.name.lower().replace(' ', '_')}_task"
        focus_desc = f"Waiting for {ctx.target_agent.name} to complete delegated task: {ctx.message_text[:100]}"

        try:
            await agent_tools._append_focus_item(ctx.source_agent.id, focus_id, focus_desc)
        except Exception as error:
            logger.warning(f"[A2A] Failed to write focus for delegate: {error}")

        trigger_name = f"a2a_wait_{ctx.target_agent.name.lower().replace(' ', '_')}"
        trigger_reason = (
            f"{ctx.target_agent.name} has replied with the result of a delegated task. "
            + f"Original task: {ctx.message_text[:200]}. "
            + f"Steps: 1) Process {ctx.target_agent.name}'s reply. "
            + f"2) Mark focus item '{focus_id}' as completed. "
            + "3) Cancel this trigger. "
            + "USER-FACING OUTPUT RULES: Your reply goes directly to the user's chat. "
            + "Write in natural, conversational language as if talking to a colleague. "
            + "NEVER use technical terms like: trigger name, focus item, a2a_wait, "
            + "task_delegate, focus_ref, or any internal identifier. "
            + "NEVER mention your internal operations (canceling triggers, updating focus, "
            + "marking items complete, trigger status, etc.). "
            + "Just summarize the task result in plain language."
        )
        try:
            await agent_tools._create_on_message_trigger(
                agent_id=ctx.source_agent.id,
                trigger_name=trigger_name,
                from_agent_name=ctx.target_agent.name,
                reason=trigger_reason,
                focus_ref=focus_id,
                notification_summary=f"Waiting for {ctx.target_agent.name} to complete the task and reply",
                origin_session_id=ctx.origin_session_id,
                origin_user_id=str(ctx.owner_id) if ctx.owner_id else None,
                origin_source_channel=ctx.origin_source_channel,
            )
        except Exception as error:
            logger.warning(f"[A2A] Failed to create trigger for delegate: {error}")

        with suppress(Exception):
            from app.services.activity_logger import log_activity

            await log_activity(
                ctx.source_agent.id,
                "agent_msg_sent",
                f"Delegated task to {ctx.target_agent.name}",
                detail={
                    "partner": ctx.target_agent.name,
                    "message": ctx.message_text[:200],
                    "msg_type": "task_delegate",
                },
            )

        try:
            await agent_tools._wake_agent_async(
                ctx.target_agent.id,
                f"[From {ctx.source_agent.name}] {ctx.message_text}",
                from_agent_id=ctx.source_agent.id,
                skip_dedup=True,
                a2a_session_id=ctx.chat_session_id,
            )
        except Exception as error:
            logger.warning(f"[A2A] Failed to wake {ctx.target_agent.name} for delegate: {error}")

        return f"✅ Task delegated to {ctx.target_agent.name}. You will be notified when they complete it."
    except Exception as error:
        logger.exception(
            f"[A2A] _a2a_handle_task_delegate failed: from={ctx.source_agent.id}, to={ctx.target_agent.id}"
        )
        return f"❌ Task delegation error ({type(error).__name__}): {str(error)[:200]}"


async def _a2a_handle_consult(ctx: A2AContext) -> str:
    try:
        suffix = (
            "\n\n--- Agent-to-Agent Message ---\n"
            + "You are receiving a message from another digital employee. "
            + "Reply concisely and helpfully. Focus on the request and provide a clear answer.\n"
            + "\n🔴 **RESPONSE PROTOCOL - MANDATORY:**\n"
            + 'You MUST call `finish(content="...")` with your complete answer. '
            + "Do NOT output plain text without calling `finish`. "
            + "Plain text responses will be REJECTED and you will be asked to redo.\n"
            + "\n** CRITICAL FILE DELIVERY RULE **\n"
            + "After you write any file (report, document, analysis, etc.) that the requesting agent needs, "
            + f'you MUST call `send_file_to_agent(agent_name="{ctx.source_agent.name}", file_path="<path>")` '
            + "to deliver it. The other agent CANNOT access your workspace. "
            + "Never just tell them the path - always deliver explicitly.\n"
        )

        conversation_messages = list(ctx.conversation_history)
        conversation_messages.append({"role": "user", "content": f"[From {ctx.source_agent.name}] {ctx.message_text}"})

        from app.services.llm.caller import call_llm_with_failover
        from app.services.llm.router import select_turn_model
        from app.services.llm.turn import ModelBundle

        choice = await select_turn_model(
            ModelBundle(
                primary=ctx.primary_model,
                secondary=ctx.secondary_model,
                fallback=ctx.fallback_model,
            ),
            user_text=ctx.message_text,
            history=ctx.conversation_history,
            agent_id=ctx.target_agent.id,
        )
        selected = choice.model
        if selected is None:
            return f"⚠️ {ctx.target_agent.name} has no LLM model configured"

        target_reply = await call_llm_with_failover(
            primary_model=selected,
            fallback_model=choice.failover_model,
            messages=conversation_messages,
            agent_name=ctx.target_agent.name,
            role_description=ctx.target_agent.role_description or "",
            agent_id=ctx.target_agent.id,
            user_id=ctx.owner_id,
            session_id=ctx.chat_session_id,
            current_user_name_override=ctx.source_agent.name,
            system_prompt_suffix=suffix,
        )

        if not target_reply or target_reply.startswith(("⚠️", "[Error]", "[LLM Error]", "[LLM call error]")):
            return target_reply or f"⚠️ {ctx.target_agent.name} did not respond (LLM returned empty)"

        tgt_part = await participant_dao.get_by_type_ref("agent", ctx.target_agent.id)
        _ = await chat_message_dao.insert_message(
            agent_id=ctx.session_agent_id,
            user_id=ctx.owner_id,
            role="assistant",
            content=target_reply,
            conversation_id=ctx.chat_session_id,
            participant_id=tgt_part.id if tgt_part else None,
        )

        from app.services.activity_logger import log_activity

        await log_activity(
            ctx.target_agent.id,
            "agent_msg_sent",
            f"Replied to message from {ctx.source_agent.name}",
            detail={"partner": ctx.source_agent.name, "message": ctx.message_text[:200], "reply": target_reply[:200]},
        )
        await log_activity(
            ctx.source_agent.id,
            "agent_msg_sent",
            f"Sent message to {ctx.target_agent.name} and received reply",
            detail={"partner": ctx.target_agent.name, "message": ctx.message_text[:200], "reply": target_reply[:200]},
        )

        return f"💬 {ctx.target_agent.name} replied:\n{target_reply}"

    except Exception as error:
        logger.exception(f"[A2A] _a2a_handle_consult failed: from={ctx.source_agent.id}, to={ctx.target_agent.id}")
        return f"❌ Consult request error ({type(error).__name__}): {str(error)[:200]}"
