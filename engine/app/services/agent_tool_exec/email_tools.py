from __future__ import annotations

import uuid
from pathlib import Path
from typing import TYPE_CHECKING

from app.core.json_types import JsonObject
from app.core.tool_types import ToolConfigSchema

from .registry import ToolArguments

if TYPE_CHECKING:
    from app.services.email_service import EmailConfig

SENSITIVE_FIELD_KEYS = {"api_key", "private_key", "auth_code", "password", "secret", "atlassian_api_key"}


def _string_argument(arguments: ToolArguments, name: str, default: str = "") -> str:
    value = arguments.get(name)
    return value if isinstance(value, str) else default


def _optional_string_argument(arguments: ToolArguments, name: str) -> str | None:
    value = arguments.get(name)
    return value if isinstance(value, str) else None


def _integer_argument(arguments: ToolArguments, name: str, default: int) -> int:
    value = arguments.get(name)
    return value if isinstance(value, int) and not isinstance(value, bool) else default


def _string_list_argument(arguments: ToolArguments, name: str) -> list[str] | None:
    value = arguments.get(name)
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return [item for item in value if isinstance(item, str)]
    return None


def _decrypt_sensitive_fields(config: JsonObject, config_schema: ToolConfigSchema | None = None) -> JsonObject:
    """Decrypt sensitive fields in config dict.

    When config_schema is provided, also decrypts fields with type='password'
    (e.g. smithery_api_key) that are not in the hardcoded SENSITIVE_FIELD_KEYS.
    """
    if not config:
        return config

    from app.config import get_settings
    from app.core.security import decrypt_data

    settings = get_settings()
    result = dict(config)

    sensitive_keys = set(SENSITIVE_FIELD_KEYS)
    if config_schema:
        for field in config_schema.get("fields", []):
            if field.get("type") == "password":
                key = field.get("key", "")
                if key:
                    sensitive_keys.add(key)

    for key in sensitive_keys:
        if result.get(key):
            value = result[key]
            if isinstance(value, str) and value:
                try:
                    result[key] = decrypt_data(value, settings.SECRET_KEY)
                except Exception:
                    result[key] = value

    return result


def _email_service_config(config: JsonObject) -> EmailConfig:
    result: EmailConfig = {}
    for name in ("email_provider", "email_address", "auth_code", "imap_host", "smtp_host"):
        value = config.get(name)
        if isinstance(value, str):
            result[name] = value
    for name in ("imap_port", "smtp_port"):
        value = config.get(name)
        if isinstance(value, str) or (isinstance(value, int) and not isinstance(value, bool)):
            result[name] = value
    smtp_ssl = config.get("smtp_ssl")
    if isinstance(smtp_ssl, bool):
        result["smtp_ssl"] = smtp_ssl
    return result


async def _get_email_config(agent_id: uuid.UUID) -> JsonObject:
    """Retrieve per-agent email config from the send_email tool's AgentTool config."""
    from app.dao.tool_dao import agent_tool_dao, tool_dao

    tool = await tool_dao.get_by_name("send_email")
    if not tool:
        return {}

    assignment = await agent_tool_dao.get_assignment(agent_id, tool.id)
    agent_config = (assignment.config or {}) if assignment else {}
    merged = {**(tool.config or {}), **agent_config}
    return _decrypt_sensitive_fields(merged, tool.config_schema)


async def _handle_email_tool(tool_name: str, agent_id: uuid.UUID, ws: Path, arguments: ToolArguments) -> str:
    """Dispatch email tool calls to the email_service module."""
    from app.services.email_service import read_emails, reply_email, send_email

    config = await _get_email_config(agent_id)
    if not config.get("email_address") or not config.get("auth_code"):
        return (
            "❌ Email not configured for this agent.\n\n"
            "Please go to Agent → Tools → Send Email → Config to set up your email:\n"
            "1. Select your email provider\n"
            "2. Enter your email address\n"
            "3. Enter your authorization code (not your login password)"
        )

    try:
        if tool_name == "send_email":
            return await send_email(
                config=_email_service_config(config),
                to=_string_argument(arguments, "to"),
                subject=_string_argument(arguments, "subject"),
                body=_string_argument(arguments, "body"),
                cc=_optional_string_argument(arguments, "cc"),
                attachments=_string_list_argument(arguments, "attachments"),
                workspace_path=ws,
                agent_id=agent_id,
            )
        if tool_name == "read_emails":
            return await read_emails(
                config=_email_service_config(config),
                limit=_integer_argument(arguments, "limit", 10),
                search=_optional_string_argument(arguments, "search"),
                folder=_string_argument(arguments, "folder", "INBOX"),
            )
        if tool_name == "reply_email":
            return await reply_email(
                config=_email_service_config(config),
                message_id=_string_argument(arguments, "message_id"),
                body=_string_argument(arguments, "body"),
            )
        return f"❌ Unknown email tool: {tool_name}"
    except Exception as e:
        return f"❌ Email tool error: {str(e)[:200]}"
