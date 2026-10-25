"""Agent activity actions (distinct from open audit action names)."""

from enum import StrEnum


class ActivityAction(StrEnum):
    CHAT_REPLY = "chat_reply"
    TOOL_CALL = "tool_call"
    FEISHU_MSG_SENT = "feishu_msg_sent"
    AGENT_MSG_SENT = "agent_msg_sent"
    WEB_MSG_SENT = "web_msg_sent"
    TASK_CREATED = "task_created"
    TASK_UPDATED = "task_updated"
    FILE_WRITTEN = "file_written"
    ERROR = "error"
    SCHEDULE_RUN = "schedule_run"
    HEARTBEAT = "heartbeat"
    PLAZA_POST = "plaza_post"
