from __future__ import annotations

import importlib
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core.json_types import JsonObject
from app.services import agent_tools
from app.services.agent_tool_exec import dispatcher, email_tools
from app.services.agent_tool_exec.registry import ToolArguments

type EmailServiceCallValue = JsonObject | str | int | list[str] | Path | uuid.UUID | None
type EmailServiceCall = dict[str, EmailServiceCallValue]
type FacadeCallValue = str | uuid.UUID | Path | JsonObject


def install_fake_email_service(monkeypatch: pytest.MonkeyPatch, **functions: object) -> None:
    monkeypatch.setitem(sys.modules, "app.services.email_service", SimpleNamespace(**functions))


@pytest.mark.asyncio
async def test_get_email_config_returns_empty_when_send_email_tool_is_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    from unittest.mock import AsyncMock

    from app.dao.tool_dao import tool_dao

    monkeypatch.setattr(tool_dao, "get_by_name", AsyncMock(return_value=None))

    result = await email_tools._get_email_config(uuid.uuid4())

    assert result == {}


@pytest.mark.asyncio
async def test_get_email_config_merges_agent_values_and_decrypts_schema_password_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import AsyncMock

    from app import config
    from app.core import security
    from app.dao.tool_dao import agent_tool_dao, tool_dao

    tool = SimpleNamespace(
        id=uuid.uuid4(),
        config={
            "email_address": "global@example.test",
            "auth_code": "encrypted-global-code",
            "smtp_token": "encrypted-global-token",
            "provider": "global-provider",
        },
        config_schema={"fields": [{"key": "smtp_token", "type": "password"}]},
    )
    agent_tool = SimpleNamespace(
        config={
            "email_address": "agent@example.test",
            "auth_code": "encrypted-agent-code",
            "smtp_token": "encrypted-agent-token",
        }
    )
    monkeypatch.setattr(tool_dao, "get_by_name", AsyncMock(return_value=tool))
    monkeypatch.setattr(agent_tool_dao, "get_assignment", AsyncMock(return_value=agent_tool))
    monkeypatch.setattr(config, "get_settings", lambda: SimpleNamespace(SECRET_KEY="test-key"))
    monkeypatch.setattr(security, "decrypt_data", lambda value, _: f"plain:{value}")

    result = await email_tools._get_email_config(uuid.uuid4())

    assert result == {
        "email_address": "agent@example.test",
        "auth_code": "plain:encrypted-agent-code",
        "smtp_token": "plain:encrypted-agent-token",
        "provider": "global-provider",
    }


@pytest.mark.asyncio
async def test_handle_email_tool_returns_configuration_guidance_before_service_call(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    async def missing_config(_: uuid.UUID) -> JsonObject:
        return {}

    async def unexpected_service_call(**_: object) -> str:
        raise AssertionError("email service must not run without credentials")

    monkeypatch.setattr(email_tools, "_get_email_config", missing_config)
    install_fake_email_service(
        monkeypatch,
        send_email=unexpected_service_call,
        read_emails=unexpected_service_call,
        reply_email=unexpected_service_call,
    )

    result = await email_tools._handle_email_tool("send_email", uuid.uuid4(), tmp_path, {})

    assert result == (
        "❌ Email not configured for this agent.\n\n"
        "Please go to Agent → Tools → Send Email → Config to set up your email:\n"
        "1. Select your email provider\n"
        "2. Enter your email address\n"
        "3. Enter your authorization code (not your login password)"
    )


@pytest.mark.asyncio
async def test_handle_email_tool_sends_workspace_cc_and_attachments(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[EmailServiceCall] = []
    agent_id = uuid.uuid4()
    arguments: ToolArguments = {
        "to": "to@example.test",
        "subject": "Subject",
        "body": "Body",
        "cc": "cc@example.test",
        "attachments": ["workspace/report.txt"],
    }

    async def configured(_: uuid.UUID) -> JsonObject:
        return {"email_address": "agent@example.test", "auth_code": "code"}

    async def send_email(**kwargs: EmailServiceCallValue) -> str:
        calls.append(kwargs)
        return "sent"

    async def unused_service(**_: object) -> str:
        raise AssertionError("unexpected email service call")

    monkeypatch.setattr(email_tools, "_get_email_config", configured)
    install_fake_email_service(
        monkeypatch, send_email=send_email, read_emails=unused_service, reply_email=unused_service
    )

    result = await email_tools._handle_email_tool("send_email", agent_id, tmp_path, arguments)

    assert result == "sent"
    assert calls == [
        {
            "config": {"email_address": "agent@example.test", "auth_code": "code"},
            "to": "to@example.test",
            "subject": "Subject",
            "body": "Body",
            "cc": "cc@example.test",
            "attachments": ["workspace/report.txt"],
            "workspace_path": tmp_path,
            "agent_id": agent_id,
        }
    ]


@pytest.mark.asyncio
async def test_handle_email_tool_reads_with_committed_defaults(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list[EmailServiceCall] = []

    async def configured(_: uuid.UUID) -> JsonObject:
        return {"email_address": "agent@example.test", "auth_code": "code"}

    async def read_emails(**kwargs: EmailServiceCallValue) -> str:
        calls.append(kwargs)
        return "read"

    async def unused_service(**_: object) -> str:
        raise AssertionError("unexpected email service call")

    monkeypatch.setattr(email_tools, "_get_email_config", configured)
    install_fake_email_service(
        monkeypatch, send_email=unused_service, read_emails=read_emails, reply_email=unused_service
    )

    result = await email_tools._handle_email_tool("read_emails", uuid.uuid4(), tmp_path, {})

    assert result == "read"
    assert calls == [
        {
            "config": {"email_address": "agent@example.test", "auth_code": "code"},
            "limit": 10,
            "search": None,
            "folder": "INBOX",
        }
    ]


@pytest.mark.asyncio
async def test_handle_email_tool_replies_with_message_id_and_body(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[EmailServiceCall] = []

    async def configured(_: uuid.UUID) -> JsonObject:
        return {"email_address": "agent@example.test", "auth_code": "code"}

    async def reply_email(**kwargs: EmailServiceCallValue) -> str:
        calls.append(kwargs)
        return "replied"

    async def unused_service(**_: object) -> str:
        raise AssertionError("unexpected email service call")

    monkeypatch.setattr(email_tools, "_get_email_config", configured)
    install_fake_email_service(
        monkeypatch, send_email=unused_service, read_emails=unused_service, reply_email=reply_email
    )

    result = await email_tools._handle_email_tool(
        "reply_email",
        uuid.uuid4(),
        tmp_path,
        {"message_id": "<message@example.test>", "body": "Reply body"},
    )

    assert result == "replied"
    assert calls == [
        {
            "config": {"email_address": "agent@example.test", "auth_code": "code"},
            "message_id": "<message@example.test>",
            "body": "Reply body",
        }
    ]


@pytest.mark.asyncio
async def test_handle_email_tool_returns_committed_unknown_tool_message(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    async def configured(_: uuid.UUID) -> JsonObject:
        return {"email_address": "agent@example.test", "auth_code": "code"}

    async def unused_service(**_: object) -> str:
        raise AssertionError("unexpected email service call")

    monkeypatch.setattr(email_tools, "_get_email_config", configured)
    install_fake_email_service(
        monkeypatch,
        send_email=unused_service,
        read_emails=unused_service,
        reply_email=unused_service,
    )

    result = await email_tools._handle_email_tool("unknown_email_tool", uuid.uuid4(), tmp_path, {})

    assert result == "❌ Unknown email tool: unknown_email_tool"


@pytest.mark.asyncio
async def test_handle_email_tool_truncates_service_errors_to_200_characters(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    async def configured(_: uuid.UUID) -> JsonObject:
        return {"email_address": "agent@example.test", "auth_code": "code"}

    async def send_email(**_: object) -> str:
        raise RuntimeError("x" * 250)

    async def unused_service(**_: object) -> str:
        raise AssertionError("unexpected email service call")

    monkeypatch.setattr(email_tools, "_get_email_config", configured)
    install_fake_email_service(
        monkeypatch, send_email=send_email, read_emails=unused_service, reply_email=unused_service
    )

    result = await email_tools._handle_email_tool("send_email", uuid.uuid4(), tmp_path, {})

    assert result == f"❌ Email tool error: {'x' * 200}"


@pytest.mark.asyncio
async def test_email_facade_forwards_the_original_arguments_object(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    received: list[FacadeCallValue] = []
    agent_id = uuid.uuid4()
    arguments: JsonObject = {"attachments": ["workspace/report.txt"]}

    async def handle(tool_name: str, actual_agent_id: uuid.UUID, workspace: Path, actual_arguments: JsonObject) -> str:
        received.extend([tool_name, actual_agent_id, workspace, actual_arguments])
        return "forwarded"

    monkeypatch.setattr(email_tools, "_handle_email_tool", handle)

    result = await agent_tools._handle_email_tool("send_email", agent_id, tmp_path, arguments)

    assert result == "forwarded"
    assert received == ["send_email", agent_id, tmp_path, arguments]
    assert received[3] is arguments


@pytest.mark.asyncio
async def test_email_config_facade_defers_to_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    agent_id = uuid.uuid4()

    async def get_config(actual_agent_id: uuid.UUID) -> JsonObject:
        assert actual_agent_id == agent_id
        return {"email_address": "agent@example.test"}

    monkeypatch.setattr(email_tools, "_get_email_config", get_config)

    result = await agent_tools._get_email_config(agent_id)

    assert result == {"email_address": "agent@example.test"}


@pytest.mark.asyncio
async def test_dispatcher_routes_literal_email_tool_to_facade(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    agent_id = uuid.uuid4()
    user_id = uuid.uuid4()
    arguments: ToolArguments = {"to": "to@example.test"}
    calls: list[FacadeCallValue] = []

    async def tenant_lookup(_: uuid.UUID) -> str:
        return "tenant"

    async def handle(tool_name: str, actual_agent_id: uuid.UUID, workspace: Path, actual_arguments: JsonObject) -> str:
        calls.extend([tool_name, actual_agent_id, workspace, actual_arguments])
        return "dispatched"

    async def log_activity(*_: object, **__: object) -> None:
        return None

    facade = SimpleNamespace(
        FINISH_TOOL_NAME="finish",
        _get_agent_tenant_id=tenant_lookup,
        _agent_workspace_root=lambda _: tmp_path,
        resolve_tool_handler=lambda _: None,
        _handle_email_tool=handle,
    )
    monkeypatch.setattr(dispatcher, "agent_tools", facade)
    monkeypatch.setattr(dispatcher, "_TOOL_AUTONOMY_MAP", {})
    monkeypatch.setitem(sys.modules, "app.services.activity_logger", SimpleNamespace(log_activity=log_activity))

    result = await dispatcher.execute_tool("send_email", arguments, agent_id, user_id)

    assert result == "dispatched"
    assert calls == ["send_email", agent_id, tmp_path, arguments]
    assert calls[3] is arguments


@pytest.mark.asyncio
async def test_direct_execution_keeps_email_tools_unsupported(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def tenant_lookup(_: uuid.UUID) -> str:
        return "tenant"

    monkeypatch.setattr(agent_tools, "_get_agent_tenant_id", tenant_lookup)
    monkeypatch.setattr(agent_tools, "_agent_workspace_root", lambda _: tmp_path)

    result = await agent_tools._execute_tool_direct("send_email", {}, uuid.uuid4())

    assert result == "Tool send_email does not support post-approval execution"


def test_importing_email_owner_does_not_import_email_service(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delitem(sys.modules, "app.services.email_service", raising=False)

    importlib.reload(email_tools)

    assert "app.services.email_service" not in sys.modules
