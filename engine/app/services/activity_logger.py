"""Activity logger - simple async function to record agent actions."""

import uuid

from app.core.json_types import JsonObject
from app.core.logging import logger
from app.dao.activity_log_dao import agent_activity_log_dao


async def log_activity(
    agent_id: uuid.UUID,
    action_type: str,
    summary: str,
    detail: JsonObject | None = None,
    related_id: uuid.UUID | None = None,
) -> None:
    """Record an agent activity. Fire-and-forget, never raises."""
    try:
        _ = await agent_activity_log_dao.create(
            obj_in={
                "agent_id": agent_id,
                "action_type": action_type,
                "summary": summary[:500] if summary else "",
                "detail_json": detail,
                "related_id": related_id,
            }
        )
    except Exception as e:
        logger.error(f"[ActivityLog] Failed to log {action_type}: {e}")
