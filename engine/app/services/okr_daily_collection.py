"""Daily OKR collection service.

Handles reminder outreach to the OKR Agent's tracked relationship network.
Human members and tracked digital employees are both expected to reply back to
the OKR Agent, which then records the report through the standard tool path.
"""

from __future__ import annotations

import re
import uuid
from datetime import date
from typing import TypedDict

from app.dao.agent_agent_relationship_dao import agent_agent_relationship_dao
from app.dao.agent_dao import agent_dao
from app.dao.agent_relationship_dao import agent_relationship_dao
from app.dao.chat_dao import chat_session_dao
from app.dao.okr_settings_dao import okr_settings_dao
from app.dao.trigger_dao import agent_trigger_dao
from app.dao.user_dao import user_dao
from app.services.agent_tool_exec.a2a_send import _send_message_to_agent
from app.services.agent_tool_exec.channel_messaging import _send_channel_message
from app.services.agent_tool_exec.platform_messaging import _send_platform_message

_LEGACY_WAIT_DAILY_REPLY = re.compile(r"^wait_.*daily_reply$")


class DailyCollectionResult(TypedDict):
    okr_agent_id: str
    human_targets: int
    agent_targets: int
    sent_humans: int
    sent_agents: int
    total_targets: int
    report_date: str


def _human_request_message(target_name: str, report_day: date) -> str:
    return (
        f"你好，{target_name}！我是 OKR Agent，需要收集你今天的日报（{report_day.isoformat()}）。请回复以下内容：\n"
        "- 今天取得的进展\n"
        "- 遇到的风险或阻碍\n"
        "- 下一步计划\n\n"
        "我收到后会帮你整理并记入 OKR 日报。谢谢！"
    )


def _agent_request_message(target_name: str, report_day: date) -> str:
    return (
        f"Hi {target_name}, this is OKR Agent collecting your daily report for {report_day.isoformat()}.\n"
        "Please review today's progress and reply to me with:\n"
        "- progress made today\n"
        "- risks or blockers\n"
        "- next step\n\n"
        "Please keep the final reply concise so I can record it directly."
    )


def _is_legacy_daily_reply_trigger(name: str) -> bool:
    """Match legacy daily_reply_* and wait_%daily_reply trigger names."""
    return name.startswith("daily_reply_") or bool(_LEGACY_WAIT_DAILY_REPLY.fullmatch(name))


async def _cleanup_legacy_daily_reply_triggers(okr_agent_id: uuid.UUID) -> None:
    """Disable legacy daily reply triggers from previous implementations."""
    triggers = await agent_trigger_dao.list_for_agent(okr_agent_id)
    for trigger in triggers:
        if not trigger.is_enabled:
            continue
        if _is_legacy_daily_reply_trigger(trigger.name):
            await agent_trigger_dao.update(db_obj=trigger, obj_in={"is_enabled": False})


async def trigger_daily_collection_for_tenant(tenant_id: uuid.UUID) -> DailyCollectionResult:
    """Send daily collection requests to tracked relationships."""
    settings = await okr_settings_dao.get_by_tenant(tenant_id)
    if not settings or not settings.enabled:
        raise ValueError("OKR is not enabled for this tenant")
    if not settings.daily_report_enabled:
        raise ValueError("Daily report collection is not enabled for this tenant")
    if not settings.okr_agent_id:
        raise ValueError("OKR Agent not found for this tenant")

    okr_agent = await agent_dao.get(settings.okr_agent_id)
    if not okr_agent:
        raise ValueError("OKR Agent not found for this tenant")

    await _cleanup_legacy_daily_reply_triggers(okr_agent.id)

    rel_rows = await agent_relationship_dao.list_for_agent_with_members(okr_agent.id, active_only=True)
    tracked_agents = await agent_agent_relationship_dao.list_target_agents(
        okr_agent.id,
        exclude_system=True,
        exclude_statuses=["stopped", "error"],
    )

    member_user_ids: dict[uuid.UUID, uuid.UUID | None] = {}
    member_user_display_names: dict[uuid.UUID, str] = {}
    for rel in rel_rows:
        org_member = rel.member
        member_user_ids[org_member.id] = org_member.user_id
        if org_member.user_id:
            user = await user_dao.get(org_member.user_id)
            if user and user.display_name:
                member_user_display_names[org_member.id] = user.display_name

        if not org_member.user_id:
            patterns: list[str] = []
            if org_member.open_id:
                patterns.append(f"feishu_p2p_{org_member.open_id}")
            if org_member.external_id:
                patterns.append(f"feishu_p2p_{org_member.external_id}")
                patterns.append(f"dingtalk_p2p_{org_member.external_id}")
            found: uuid.UUID | None = None
            for pattern in patterns:
                sess = await chat_session_dao.get_by_external_conv(
                    agent_id=okr_agent.id,
                    external_conv_id=pattern,
                )
                if sess and sess.user_id:
                    found = sess.user_id
                    break
            if found:
                member_user_ids[org_member.id] = found
                user = await user_dao.get(found)
                if user and user.display_name:
                    member_user_display_names[org_member.id] = user.display_name

    report_day = date.today()  # noqa: DTZ011
    sent_humans = 0
    sent_agents = 0

    for rel in rel_rows:
        org_member = rel.member
        platform_name = member_user_display_names.get(org_member.id)
        message_text = _human_request_message(org_member.name, report_day)
        has_external_channel = bool(org_member.open_id or org_member.external_id)

        send_result = ""
        if has_external_channel:
            send_result = await _send_channel_message(
                okr_agent.id,
                {"member_name": org_member.name, "message": message_text},
            )
        elif platform_name:
            send_result = await _send_platform_message(
                okr_agent.id,
                {"username": platform_name, "message": message_text},
            )

        if send_result.startswith("✅"):
            sent_humans += 1

    for agent_member in tracked_agents:
        send_result = await _send_message_to_agent(
            okr_agent.id,
            {
                "agent_name": agent_member.name,
                "message": _agent_request_message(agent_member.name, report_day),
                "msg_type": "task_delegate",
                "force_async": True,
            },
        )
        if send_result.startswith("✅"):
            sent_agents += 1

    return {
        "okr_agent_id": str(okr_agent.id),
        "human_targets": len(rel_rows),
        "agent_targets": len(tracked_agents),
        "sent_humans": sent_humans,
        "sent_agents": sent_agents,
        "total_targets": len(rel_rows) + len(tracked_agents),
        "report_date": report_day.isoformat(),
    }
