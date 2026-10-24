"""The shared wire projection for legacy and paged chat history."""

import json
import re
from collections.abc import Sequence
from typing import NotRequired, TypedDict

from app.core.json_types import JsonValue, is_json_value, json_as_str_or, json_loads_object, json_loads_value
from app.dao.chat_dao import chat_message_dao
from app.records.chat import ChatMessageRecord
from app.records.chat_attachment import AttachmentRef


class _ChatMessageContent(TypedDict):
    role: str
    content: str
    created_at: NotRequired[str | None]
    toolName: NotRequired[str]
    toolArgs: NotRequired[JsonValue]
    toolStatus: NotRequired[str]
    toolResult: NotRequired[str]
    toolThinking: NotRequired[str]
    thinking: NotRequired[str]
    sender_name: NotRequired[str]
    participant_id: NotRequired[str]


class ChatMessageEntry(_ChatMessageContent):
    message_id: str
    entry_id: str
    client_submission_id: NotRequired[str | None]
    attachments: NotRequired[list[AttachmentRef]]


async def project_messages(messages: Sequence[ChatMessageRecord], source_channel: str) -> list[ChatMessageEntry]:
    sender_cache = (
        await chat_message_dao.sender_names(list({m.participant_id for m in messages if m.participant_id}))
        if source_channel == "agent"
        else {}
    )
    out: list[ChatMessageEntry] = []
    for m in messages:
        sender_name = sender_cache.get(str(m.participant_id)) if m.participant_id else None
        if m.role == "tool_call":
            entry: ChatMessageEntry = {
                "message_id": str(m.id),
                "entry_id": str(m.id),
                "role": m.role,
                "content": m.content,
                "created_at": m.created_at.isoformat() if m.created_at else None,
            }
            try:
                data = json_loads_object(m.content)
                entry["content"] = ""
                entry["toolName"] = json_as_str_or(data.get("name")) or json_as_str_or(data.get("tool_name"))
                entry["toolArgs"] = data.get("args") if data.get("args") is not None else data.get("arguments")
                entry["toolStatus"] = json_as_str_or(data.get("status"), "done")
                entry["toolResult"] = json_as_str_or(data.get("result"))
                entry["toolThinking"] = json_as_str_or(data.get("reasoning_content"))
            except json.JSONDecodeError, TypeError, AttributeError:
                entry["content"] = m.content
            if sender_name not in (None, ""):
                entry["sender_name"] = sender_name
            out.append(entry)
            continue

        if source_channel == "agent" and m.role == "assistant" and "```tool_code" in (m.content or ""):
            for index, part in enumerate(_split_inline_tools(m.content)):
                if sender_name not in (None, ""):
                    part["sender_name"] = sender_name
                if m.participant_id:
                    part["participant_id"] = str(m.participant_id)
                out.append({**part, "message_id": str(m.id), "entry_id": f"{m.id}:{index}"})
        else:
            message_entry: ChatMessageEntry = {
                "message_id": str(m.id),
                "entry_id": str(m.id),
                "role": m.role,
                "content": m.content,
                "created_at": m.created_at.isoformat() if m.created_at else None,
            }
            if m.role == "user":
                message_entry["client_submission_id"] = str(m.client_submission_id) if m.client_submission_id else None
            if m.attachments is not None and len(m.attachments) > 0:
                message_entry["attachments"] = [ref.as_dict() for ref in m.attachments]
            if m.thinking not in (None, ""):
                message_entry["thinking"] = m.thinking
            if sender_name not in (None, ""):
                message_entry["sender_name"] = sender_name
            if m.participant_id:
                message_entry["participant_id"] = str(m.participant_id)
            out.append(message_entry)
    return out


def _split_inline_tools(content: str) -> list[_ChatMessageContent]:
    pattern = re.compile(r"```tool_code\s*\n\s*(\w+)\s*\n```" + r"(?:\s*```json\s*\n(.*?)\n```)?", re.DOTALL)
    parts: list[_ChatMessageContent] = []
    last_end = 0
    for match in pattern.finditer(content):
        text_before = content[last_end : match.start()].strip()
        if text_before:
            parts.append({"role": "assistant", "content": text_before})
        args_str = match.group(2)
        tool_args: JsonValue = None
        if args_str:
            try:
                loaded = json_loads_value(args_str.strip())
                tool_args = loaded if is_json_value(loaded) else {"raw": args_str.strip()}
            except json.JSONDecodeError:
                tool_args = {"raw": args_str.strip()}
        parts.append(
            {
                "role": "tool_call",
                "content": "",
                "toolName": match.group(1),
                "toolArgs": tool_args,
                "toolStatus": "done",
                "toolResult": "",
            }
        )
        last_end = match.end()
    trailing = content[last_end:].strip()
    if trailing:
        parts.append({"role": "assistant", "content": trailing})
    if not parts:
        parts.append({"role": "assistant", "content": content})
    return parts
