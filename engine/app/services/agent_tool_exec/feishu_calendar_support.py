from __future__ import annotations

import importlib
import re
import uuid
from datetime import UTC, datetime

from app.core.logging import logger
from app.services import agent_tools
from app.services.agent_tool_exec.channel_context import channel_feishu_sender_open_id
from app.services.agent_tool_exec.registry import ToolArguments, ToolArgumentValue
from app.services.feishu_service import FeishuService


def _feishu_service() -> FeishuService:
    return importlib.import_module("app.services.feishu_service").feishu_service


def _to_iso(value: str | None, default: datetime) -> str:
    if not value:
        return default.strftime("%Y-%m-%dT%H:%M:%S+00:00")
    if re.fullmatch(r"\d+", value.strip()):
        return datetime.fromtimestamp(int(value.strip()), tz=UTC).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    return value.strip()


def _to_unix(value: str | None, default: datetime) -> str:
    if not value:
        return str(int(default.timestamp()))
    if re.fullmatch(r"\d+", value.strip()):
        return value.strip()
    try:
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            return str(int(parsed.timestamp()))
        except ValueError:
            pass
        date_parser = importlib.import_module("dateutil.parser")
        return str(int(date_parser.parse(value).timestamp()))
    except Exception:
        return str(int(default.timestamp()))


def _nested_string(value: ToolArgumentValue | None, name: str) -> str:
    if not isinstance(value, dict):
        return ""
    nested = value.get(name)
    return nested if isinstance(nested, str) else ""


def _format_calendar_items(items: list[dict[str, ToolArgumentValue]]) -> list[str]:
    lines: list[str] = []
    if items:
        lines.append(f"📅 Bot 日历共 {len(items)} 个日程：\n")
    for event in items:
        summary = event.get("summary")
        summary_text = summary if isinstance(summary, str) else "(no title)"
        start = _nested_string(event.get("start_time"), "timestamp")
        end_time = _nested_string(event.get("end_time"), "timestamp")
        location = _nested_string(event.get("location"), "name")
        event_id = event.get("event_id")
        event_id_text = event_id if isinstance(event_id, str) else ""
        try:
            start_text = datetime.fromtimestamp(int(start), tz=UTC).strftime("%m-%d %H:%M") if start else "?"
            end_text = datetime.fromtimestamp(int(end_time), tz=UTC).strftime("%H:%M") if end_time else "?"
        except Exception:
            start_text, end_text = start, end_time
        location_text = f" | 📍{location}" if location else ""
        lines.append(f"- **{summary_text}** | 🕐{start_text}–{end_text}{location_text}  (ID: `{event_id_text}`)")  # noqa: RUF001
    return lines


async def _calendar_write_context(
    agent_id: uuid.UUID, user_email: str
) -> tuple[object, str, str, str]:
    app_id, app_secret = await agent_tools._get_feishu_credentials(agent_id)
    if not app_id or not app_secret:
        return agent_tools, "", "", "❌ Agent has no Feishu channel configured."
    token = await _feishu_service().get_tenant_access_token(app_id, app_secret)
    open_id = await agent_tools._feishu_resolve_open_id(token, user_email)
    if not open_id:
        return agent_tools, token, "", f"❌ User '{user_email}' not found."
    agent_cal_id, cal_err = await agent_tools._get_agent_calendar_id(token)
    if not agent_cal_id:
        return agent_tools, token, "", cal_err or "❌ Failed to retrieve agent's primary calendar ID."
    return agent_tools, token, agent_cal_id, ""


async def _calendar_attendees(
    agent_id: uuid.UUID, arguments: ToolArguments, token: str, user_email: str
) -> tuple[list[str], list[str]]:
    attendee_open_ids: list[str] = []
    attendee_display: list[str] = []
    attendee_open_id_values = arguments.get("attendee_open_ids")
    for open_id in attendee_open_id_values if isinstance(attendee_open_id_values, list) else []:
        if isinstance(open_id, str) and open_id and open_id not in attendee_open_ids:
            attendee_open_ids.append(open_id)
            attendee_display.append(open_id)
    attendee_name_values = arguments.get("attendee_names")
    for attendee_name_value in attendee_name_values if isinstance(attendee_name_values, list) else []:
        if not isinstance(attendee_name_value, str):
            continue
        attendee_name = attendee_name_value
        attendee_name = attendee_name.strip()
        if not attendee_name:
            continue
        search_result = await agent_tools._feishu_user_search(agent_id, {"name": attendee_name})
        match = re.search(r"open_id: `(ou_[A-Za-z0-9]+)`", search_result)
        if match and match.group(1) not in attendee_open_ids:
            attendee_open_ids.append(match.group(1))
            attendee_display.append(attendee_name)
        elif not match:
            logger.warning(f"[Calendar] Could not resolve attendee '{attendee_name}': {search_result[:100]}")
    attendee_email_values = arguments.get("attendee_emails")
    attendee_emails = (
        [email for email in attendee_email_values if isinstance(email, str)]
        if isinstance(attendee_email_values, list)
        else []
    )
    if user_email and user_email not in attendee_emails:
        attendee_emails.append(user_email)
    for email in attendee_emails[:20]:
        open_id = await agent_tools._feishu_resolve_open_id(token, email)
        if open_id and open_id not in attendee_open_ids:
            attendee_open_ids.append(open_id)
            attendee_display.append(email)
    sender_open_id = channel_feishu_sender_open_id.get(None)
    if sender_open_id and sender_open_id not in attendee_open_ids:
        attendee_open_ids.append(sender_open_id)
    return attendee_open_ids, attendee_display
