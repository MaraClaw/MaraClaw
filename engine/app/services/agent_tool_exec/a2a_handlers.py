from __future__ import annotations

from datetime import UTC, datetime

from app.core.logging import logger
from app.services.activity_logger import log_activity
from app.services.openclaw_routing import enqueue_openclaw_message

from .a2a_context import A2AContext


async def _a2a_handle_openclaw(ctx: A2AContext) -> str:
    try:
        _ = await enqueue_openclaw_message(
            agent=ctx.target_agent,
            content=f"[From {ctx.source_agent.name}] {ctx.message_text}",
            sender_agent_id=ctx.source_agent.id,
            sender_user_id=ctx.owner_id,
            conversation_id=ctx.chat_session_id,
            history=ctx.conversation_history,
            await_wake=False,
        )

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
