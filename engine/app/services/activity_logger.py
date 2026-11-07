"""Activity logger - simple async function to record agent actions."""

import uuid
from typing import assert_never

from app.core.domain.activity import ActivityAction
from app.core.json_types import JsonObject, JsonValue
from app.core.logging import logger
from app.dao.activity_log_dao import agent_activity_log_dao


def _sanitize_detail(value: JsonValue) -> JsonValue:
    match value:
        case str():
            return value.replace("\x00", "\ufffd")
        case list():
            return [_sanitize_detail(item) for item in value]
        case dict():
            return {key.replace("\x00", "\ufffd"): _sanitize_detail(item) for key, item in value.items()}
        case int() | float() | None:
            return value
        case unreachable:
            assert_never(unreachable)


async def log_activity(
    agent_id: uuid.UUID,
    action_type: ActivityAction,
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
                "summary": summary.replace("\x00", "\ufffd")[:500] if summary else "",
                "detail_json": _sanitize_detail(detail),
                "related_id": related_id,
            }
        )
    except Exception as e:
        logger.error(f"[ActivityLog] Failed to log {action_type}: {e}")
