from __future__ import annotations

import importlib
import uuid
from datetime import UTC, datetime, timedelta

import httpx

from app.core.json_types import JsonObject, json_as_str
from app.core.logging import logger
from app.services import agent_tools
from app.services.agent_tool_exec.channel_context import channel_feishu_sender_open_id
from app.services.feishu_service import FeishuService

from .registry import ToolArguments, ToolArgumentValue, tool_arg_str


def _calendar_support():
    return importlib.import_module("app.services.agent_tool_exec.feishu_calendar_support")


def _to_unix(value: str | None, default: datetime) -> str:
    return _calendar_support()._to_unix(value, default)


def _to_iso(value: str | None, default: datetime) -> str:
    return _calendar_support()._to_iso(value, default)


def _format_calendar_items(items: list[dict[str, ToolArgumentValue]]) -> list[str]:
    return _calendar_support()._format_calendar_items(items)


async def _calendar_write_context(agent_id: uuid.UUID, user_email: str) -> tuple[object, str, str, str]:
    return await _calendar_support()._calendar_write_context(agent_id, user_email)


async def _calendar_attendees(
    agent_id: uuid.UUID, arguments: ToolArguments, token: str, user_email: str
) -> tuple[list[str], list[str]]:
    return await _calendar_support()._calendar_attendees(agent_id, arguments, token, user_email)


def _feishu_service() -> FeishuService:
    return importlib.import_module("app.services.feishu_service").feishu_service


def _httpx_module():
    return importlib.import_module("httpx")


def _httpx_client(**kwargs: object) -> httpx.AsyncClient:
    return _httpx_module().AsyncClient(**kwargs)


def _response_mapping(response: httpx.Response) -> JsonObject:
    raw: object = response.json()
    return raw if isinstance(raw, dict) else {}


def _nested_mapping(value: object) -> JsonObject:
    return value if isinstance(value, dict) else {}


def _object_items(value: object) -> list[dict[str, ToolArgumentValue]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _string_argument(arguments: ToolArguments, name: str, default: str = "") -> str:
    value = arguments.get(name)
    return value if isinstance(value, str) else default


async def _get_agent_calendar_id(token: str) -> tuple[str | None, str | None]:
    async with _httpx_client(timeout=10) as client:
        response = await client.post(
            "https://open.feishu.cn/open-apis/calendar/v4/calendars/primary",
            headers={"Authorization": f"Bearer {token}"},
        )
    data = _response_mapping(response)
    code = data.get("code", -1)
    if code == 0:
        calendars = _object_items(_nested_mapping(data.get("data")).get("calendars"))
        if calendars:
            return json_as_str(_nested_mapping(calendars[0].get("calendar")).get("calendar_id")), None
        return None, "日历列表为空，请确认应用有 calendar:calendar 权限并已发布新版本"
    if code == 99991672:
        return None, (
            "❌ 飞书日历权限未开通（错误码 99991672）\n\n"
            + "请在飞书开放平台为应用 cli_a9257c5136781ceb 开通以下权限并发布新版本：\n"
            + "• calendar:calendar:readonly（应用身份权限）\n"
            + "• calendar:calendar.event:create（应用身份权限）\n"
            + "• calendar:calendar.event:read（用户身份权限）\n"
            + "• calendar:calendar.event:update（用户身份权限）\n"
            + "• calendar:calendar.event:delete（用户身份权限）\n\n"
            + "开通步骤：飞书开放平台 → 权限管理 → 批量导入权限 → 添加以上权限 → 创建版本 → 确认发布"
        )
    return None, f"获取日历 ID 失败：{data.get('msg')} (code {code})"


async def _feishu_resolve_open_id(token: str, email: str) -> str | None:
    async with _httpx_client(timeout=10) as client:
        response = await client.post(
            "https://open.feishu.cn/open-apis/contact/v3/users/batch_get_id",
            json={"emails": [email]},
            headers={"Authorization": f"Bearer {token}"},
            params={"user_id_type": "open_id"},
        )
    data = _response_mapping(response)
    if data.get("code") != 0:
        return None
    for user in _object_items(_nested_mapping(data.get("data")).get("user_list")):
        open_id = json_as_str(user.get("user_id"))
        if open_id:
            return open_id
    return None


def _iso_to_ts(iso_str: str) -> float:
    from datetime import datetime as datetime_type

    try:
        return datetime_type.fromisoformat(iso_str.replace("Z", "+00:00")).timestamp()
    except ValueError as error:
        raise ValueError(f"Cannot parse datetime: {iso_str!r}") from error


async def _feishu_calendar_list(agent_id: uuid.UUID, arguments: ToolArguments) -> str:
    user_email = _string_argument(arguments, "user_email").strip()
    app_id, app_secret = await agent_tools._get_feishu_credentials(agent_id)
    if not app_id or not app_secret:
        return "❌ Agent has no Feishu channel configured."
    token = await _feishu_service().get_tenant_access_token(app_id, app_secret)

    now = datetime.now(UTC)
    start_arg = tool_arg_str(arguments.get("start_time"))
    end_arg = tool_arg_str(arguments.get("end_time"))
    start_ts = _to_unix(start_arg, now)
    end_ts = _to_unix(end_arg, now + timedelta(days=7))
    start_iso = _to_iso(start_arg, now)
    end_iso = _to_iso(end_arg, now + timedelta(days=7))

    sender_open_id = channel_feishu_sender_open_id.get(None)
    if arguments.get("user_open_id"):
        sender_open_id = _string_argument(arguments, "user_open_id")
    elif user_email:
        resolved = await agent_tools._feishu_resolve_open_id(token, user_email)
        if resolved:
            sender_open_id = resolved

    freebusy_section = await _freebusy_section(sender_open_id, token, start_iso, end_iso) if sender_open_id else ""
    agent_cal_id, cal_err = await agent_tools._get_agent_calendar_id(token)
    if not agent_cal_id:
        if freebusy_section:
            return freebusy_section.strip()
        return cal_err or "❌ Failed to retrieve agent's primary calendar ID."

    params: dict[str, str] = {}
    if start_ts:
        params["start_time"] = start_ts
    if end_ts:
        params["end_time"] = end_ts
    async with _httpx_client(timeout=20) as client:
        response = await client.get(
            f"https://open.feishu.cn/open-apis/calendar/v4/calendars/{agent_cal_id}/events",
            headers={"Authorization": f"Bearer {token}"},
            params=params,
        )
    data = _response_mapping(response)
    if data.get("code") != 0:
        if freebusy_section:
            return freebusy_section.strip()
        return f"❌ Calendar API error: {data.get('msg')} (code {data.get('code')})"

    items = _object_items(_nested_mapping(data.get("data")).get("items"))
    if not items and not freebusy_section:
        return "📅 该时间段内没有日程。"
    lines = _format_calendar_items(items)
    if freebusy_section:
        lines.append(freebusy_section)
    return "\n".join(lines) if lines else "📅 该时间段内没有日程。"


async def _freebusy_section(sender_open_id: str, token: str, start_iso: str, end_iso: str) -> str:
    try:
        async with _httpx_client(timeout=10) as client:
            response = await client.post(
                "https://open.feishu.cn/open-apis/calendar/v4/freebusy/list",
                headers={"Authorization": f"Bearer {token}"},
                params={"user_id_type": "open_id"},
                json={"time_min": start_iso, "time_max": end_iso, "user_id": sender_open_id},
            )
        data = _response_mapping(response)
        if data.get("code") != 0:
            return ""
        busy_slots = _object_items(_nested_mapping(data.get("data")).get("freebusy_list"))
        if not busy_slots:
            return "\n📌 **用户真实日历**：该时段全部空闲。"
        from zoneinfo import ZoneInfo

        tz_cn = ZoneInfo("Asia/Shanghai")
        busy_lines = []
        for slot in sorted(busy_slots, key=lambda item: json_as_str(item.get("start_time")) or ""):
            start_raw = json_as_str(slot.get("start_time")) or ""
            end_raw = json_as_str(slot.get("end_time")) or ""
            try:
                start = datetime.fromisoformat(start_raw).astimezone(tz_cn).strftime("%H:%M")
                end = datetime.fromisoformat(end_raw).astimezone(tz_cn).strftime("%H:%M")
                busy_lines.append(f"  🔴 {start}–{end}")  # noqa: RUF001
            except Exception:
                busy_lines.append(f"  🔴 {start_raw}–{end_raw}")  # noqa: RUF001
        return "\n📌 **用户真实日历（忙碌时段）**：\n" + "\n".join(busy_lines)
    except Exception as error:
        return f"\n⚠️ Freebusy 查询异常: {error}"


async def _feishu_calendar_create(agent_id: uuid.UUID, arguments: ToolArguments) -> str:
    user_email = _string_argument(arguments, "user_email").strip()
    summary = _string_argument(arguments, "summary").strip()
    start_time = _string_argument(arguments, "start_time").strip()
    end_time = _string_argument(arguments, "end_time").strip()
    for field_name, field_value in [("summary", summary), ("start_time", start_time), ("end_time", end_time)]:
        if not field_value:
            return f"❌ Missing required argument '{field_name}'"
    app_id, app_secret = await agent_tools._get_feishu_credentials(agent_id)
    if not app_id or not app_secret:
        return "❌ Agent has no Feishu channel configured."
    token = await _feishu_service().get_tenant_access_token(app_id, app_secret)
    if user_email and not await agent_tools._feishu_resolve_open_id(token, user_email):
        logger.warning(
            f"[Feishu Calendar] Could not resolve open_id for '{user_email}', continuing without organizer invite"
        )

    agent_cal_id, cal_err = await agent_tools._get_agent_calendar_id(token)
    if not agent_cal_id:
        return cal_err or "❌ Failed to retrieve agent's primary calendar ID."

    timezone = _string_argument(arguments, "timezone", "Asia/Shanghai")
    body: dict[str, ToolArgumentValue] = {
        "summary": summary,
        "start_time": {"timestamp": str(int(agent_tools._iso_to_ts(start_time))), "timezone": timezone},
        "end_time": {"timestamp": str(int(agent_tools._iso_to_ts(end_time))), "timezone": timezone},
    }
    description = _string_argument(arguments, "description")
    if description:
        body["description"] = description
    location = _string_argument(arguments, "location")
    if location:
        body["location"] = {"name": location}

    async with _httpx_client(timeout=20) as client:
        response = await client.post(
            f"https://open.feishu.cn/open-apis/calendar/v4/calendars/{agent_cal_id}/events",
            json=body,
            headers={"Authorization": f"Bearer {token}"},
        )
    data = _response_mapping(response)
    if data.get("code") != 0:
        return f"❌ Failed to create event: {data.get('msg')} (code {data.get('code')})"
    event_id = json_as_str(_nested_mapping(_nested_mapping(data.get("data")).get("event")).get("event_id")) or ""
    attendee_open_ids, attendee_display = await _calendar_attendees(agent_id, arguments, token, user_email)

    if attendee_open_ids and event_id:
        async with _httpx_client(timeout=20) as client:
            for open_id in attendee_open_ids:
                await client.post(
                    f"https://open.feishu.cn/open-apis/calendar/v4/calendars/{agent_cal_id}/events/{event_id}/attendees",
                    json={"attendees": [{"type": "user", "user_id": open_id}]},
                    headers={"Authorization": f"Bearer {token}"},
                    params={"user_id_type": "open_id"},
                )
    attendee_text = f"\n**参与人**: {', '.join(attendee_display)}" if attendee_display else ""
    invite_note = "\n（已向您发送日历邀请，请在飞书日历中确认）" if attendee_open_ids else ""
    return f"✅ 日历事件已创建！\n**标题**: {summary}\n**时间**: {start_time} → {end_time}{attendee_text}\n**Event ID**: `{event_id}`{invite_note}"


async def _feishu_calendar_update(agent_id: uuid.UUID, arguments: ToolArguments) -> str:
    user_email = _string_argument(arguments, "user_email").strip()
    event_id = _string_argument(arguments, "event_id").strip()
    if not user_email or not event_id:
        return "❌ Both 'user_email' and 'event_id' are required."
    _facade, token, agent_cal_id, error = await _calendar_write_context(agent_id, user_email)
    if error:
        return error

    patch: dict[str, ToolArgumentValue] = {}
    timezone = _string_argument(arguments, "timezone", "Asia/Shanghai")
    summary = _string_argument(arguments, "summary")
    if summary:
        patch["summary"] = summary
    description = _string_argument(arguments, "description")
    if description:
        patch["description"] = description
    location = _string_argument(arguments, "location")
    if location:
        patch["location"] = {"name": location}
    start_time = _string_argument(arguments, "start_time")
    if start_time:
        patch["start_time"] = {"timestamp": str(int(agent_tools._iso_to_ts(start_time))), "timezone": timezone}
    end_time = _string_argument(arguments, "end_time")
    if end_time:
        patch["end_time"] = {"timestamp": str(int(agent_tools._iso_to_ts(end_time))), "timezone": timezone}
    if not patch:
        return "ℹ️ No fields to update."  # noqa: RUF001
    async with _httpx_client(timeout=20) as client:
        response = await client.patch(
            f"https://open.feishu.cn/open-apis/calendar/v4/calendars/{agent_cal_id}/events/{event_id}",
            json=patch,
            headers={"Authorization": f"Bearer {token}"},
        )
    data = _response_mapping(response)
    if data.get("code") != 0:
        return f"❌ Failed to update: {data.get('msg')} (code {data.get('code')})"
    return f"✅ Event `{event_id}` updated. Changed: {', '.join(patch.keys())}."


async def _feishu_calendar_delete(agent_id: uuid.UUID, arguments: ToolArguments) -> str:
    user_email = _string_argument(arguments, "user_email").strip()
    event_id = _string_argument(arguments, "event_id").strip()
    if not user_email or not event_id:
        return "❌ Both 'user_email' and 'event_id' are required."
    _facade, token, agent_cal_id, error = await _calendar_write_context(agent_id, user_email)
    if error:
        return error
    async with _httpx_client(timeout=20) as client:
        response = await client.delete(
            f"https://open.feishu.cn/open-apis/calendar/v4/calendars/{agent_cal_id}/events/{event_id}",
            headers={"Authorization": f"Bearer {token}"},
        )
    data = _response_mapping(response)
    if data.get("code") != 0:
        return f"❌ Failed to delete: {data.get('msg')} (code {data.get('code')})"
    return f"✅ Event `{event_id}` deleted successfully."
