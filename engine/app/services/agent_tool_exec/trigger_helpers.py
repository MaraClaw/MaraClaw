from __future__ import annotations

import importlib
import uuid
from contextlib import suppress
from types import ModuleType
from typing import Protocol, TypeIs

from app.core.json_types import datetime_from_row
from app.dao.chat_dao import chat_session_dao
from app.db.session import connection_ctx
from app.services.agent_tool_exec.registry import ToolArgumentValue


class _CroniterModule(Protocol):
    def croniter(self, expr: str) -> object: ...


def _is_croniter_module(value: object) -> TypeIs[_CroniterModule]:
    return callable(getattr(value, "croniter", None))


def _validate_trigger_config(ttype: str, config: dict[str, ToolArgumentValue]) -> str | None:
    if ttype == "cron":
        expr_value = config.get("expr", "")
        expr = expr_value if isinstance(expr_value, str) else ""
        if not expr:
            return '❌ cron trigger requires config.expr, e.g. {"expr": "0 9 * * *"}'
        try:
            croniter_module: object = importlib.import_module("croniter")
            if not _is_croniter_module(croniter_module):
                raise TypeError("croniter is unavailable")
            croniter_module.croniter(expr)
        except Exception:
            return f"❌ Invalid cron expression: '{expr}'"
    elif ttype == "once":
        if not config.get("at"):
            return '❌ once trigger requires config.at, e.g. {"at": "2026-03-10T09:00:00+08:00"}'
    elif ttype == "interval":
        if not config.get("minutes"):
            return '❌ interval trigger requires config.minutes, e.g. {"minutes": 30}'
    elif ttype == "poll":
        if not config.get("url"):
            return "❌ poll trigger requires config.url"
    elif ttype == "on_message" and not config.get("from_agent_name") and not config.get("from_user_name"):
        return "❌ on_message trigger requires config.from_agent_name (for agents) or config.from_user_name (for human users on Feishu/Slack/Discord)"
    return None


async def _snapshot_latest_message(
    facade: ModuleType, agent_id: uuid.UUID, config: dict[str, ToolArgumentValue]
) -> None:
    _ = facade
    with suppress(Exception):
        async with connection_ctx() as db:
            value = await db.fetchval(
                "SELECT m.created_at FROM chat_messages m "
                + "JOIN chat_sessions s ON m.conversation_id = s.id::text "
                + "WHERE s.agent_id = %(agent_id)s AND m.created_at IS NOT NULL "
                + "ORDER BY m.created_at DESC LIMIT 1",
                {"agent_id": agent_id},
            )
            created = datetime_from_row(value)
            if created:
                config["_since_ts"] = created.isoformat()


async def _record_origin_metadata(
    facade: ModuleType,
    agent_id: uuid.UUID,
    config: dict[str, ToolArgumentValue],
    *,
    session_id: str,
    user_id: uuid.UUID | None,
) -> None:
    _ = facade
    _ = agent_id
    if not session_id:
        return
    try:
        origin_session = await chat_session_dao.get(uuid.UUID(session_id))
        if origin_session:
            config["_origin_session_id"] = str(origin_session.id)
            config["_origin_source_channel"] = origin_session.source_channel
            if origin_session.source_channel == "agent" and origin_session.peer_agent_id:
                config["_origin_peer_agent_id"] = str(origin_session.peer_agent_id)
            elif origin_session.source_channel != "trigger":
                config["_origin_user_id"] = str(origin_session.user_id)
        elif user_id:
            config["_origin_user_id"] = str(user_id)
    except Exception:
        if user_id:
            config["_origin_user_id"] = str(user_id)


async def _write_trigger_audit(event: str, detail: dict[str, ToolArgumentValue], *, agent_id: uuid.UUID) -> None:
    with suppress(Exception):
        from app.services.audit_logger import write_audit_log

        await write_audit_log(event, detail, agent_id=agent_id)
