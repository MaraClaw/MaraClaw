from __future__ import annotations

import importlib
import uuid
from collections import deque
from types import SimpleNamespace

from app.services import agent_tools
from app.services.feishu_service import FeishuAPIError


class _FeishuSender:
    def __init__(self, responses):
        self._responses = deque(responses)
        self.calls = []

    async def send_message(self, app_id, app_secret, *, receive_id, msg_type, content, receive_id_type):
        self.calls.append((app_id, app_secret, receive_id, msg_type, content, receive_id_type))
        response = self._responses.popleft()
        if isinstance(response, BaseException):
            raise response
        return response


def _message_module():
    return importlib.import_module("app.services.agent_tool_exec.feishu_message")


def _member(**overrides):
    values = {
        "id": uuid.uuid4(),
        "name": "Alice",
        "external_id": "ou_alice",
        "open_id": "ou_alice_open",
        "email": "alice@example.test",
        "phone": "13800000000",
        "status": "active",
        "tenant_id": uuid.uuid4(),
        "user_id": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _relationship(member=None, *, status="active", reason=""):
    mem = member or _member()
    return SimpleNamespace(
        id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        member_id=mem.id,
        member=mem,
        access_status=status,
        access_status_reason=reason,
    )


def _config():
    return SimpleNamespace(app_id="app-id", app_secret="app-secret")


async def _active_status(_db, relationship, **_kwargs):
    return {
        "access_status": getattr(relationship, "access_status", "active"),
        "access_status_reason": getattr(relationship, "access_status_reason", None),
    }


def _patch_dependencies(
    monkeypatch,
    target,
    *,
    config=None,
    direct_relationship=None,
    named_relationships=None,
    sender: _FeishuSender,
):
    platform_calls = []
    session_calls = []
    message_inserts = []
    session_updates = []

    async def get_channel(**_kwargs):
        return config

    async def get_active_by_feishu(_agent_id, _feishu_id):
        return direct_relationship

    async def list_with_members(_agent_id):
        return list(named_relationships or [])

    async def platform_user(*, db, org_member, agent_tenant_id=None):
        platform_calls.append((db, org_member, agent_tenant_id))
        return SimpleNamespace(id=uuid.uuid4())

    async def channel_session(**kwargs):
        session_calls.append(kwargs)
        return SimpleNamespace(id=uuid.uuid4(), last_message_at=None)

    async def insert_message(**kwargs):
        message_inserts.append(kwargs)
        return SimpleNamespace(**kwargs)

    async def session_update(*, db_obj, obj_in):
        session_updates.append(obj_in)
        return db_obj

    async def agent_get(_id):
        return SimpleNamespace(tenant_id=uuid.uuid4())

    monkeypatch.setattr(target.channel_config_dao, "get_for_agent", get_channel)
    monkeypatch.setattr(target.agent_relationship_dao, "get_active_for_agent_by_feishu_id", get_active_by_feishu)
    monkeypatch.setattr(target.agent_relationship_dao, "list_for_agent_with_members", list_with_members)
    monkeypatch.setattr(target, "evaluate_human_relationship_status", _active_status)
    monkeypatch.setattr(target, "get_platform_user_by_org_member", platform_user)
    monkeypatch.setattr(target, "find_or_create_channel_session", channel_session)
    monkeypatch.setattr(target.chat_message_dao, "insert_message", insert_message)
    monkeypatch.setattr(target.chat_session_dao, "update", session_update)
    monkeypatch.setattr(target.agent_dao, "get", agent_get)

    feishu_service_module = importlib.import_module("app.services.feishu_service")
    monkeypatch.setattr(feishu_service_module, "feishu_service", sender)
    return platform_calls, session_calls, message_inserts


async def test_send_feishu_message_requires_message_content() -> None:
    result = await agent_tools._send_feishu_message(uuid.uuid4(), {"member_name": "Alice"})

    assert result == "❌ Please provide message content"


async def test_send_feishu_message_requires_recipient() -> None:
    result = await agent_tools._send_feishu_message(uuid.uuid4(), {"message": "hello"})

    assert result == "❌ Please provide member_name or user_id"


async def test_send_feishu_message_reports_missing_feishu_config(monkeypatch) -> None:
    target = _message_module()
    sender = _FeishuSender([])
    _patch_dependencies(monkeypatch, target, config=None, sender=sender)

    result = await agent_tools._send_feishu_message(uuid.uuid4(), {"member_name": "Alice", "message": "hello"})

    assert result == "❌ This agent has no Feishu channel configured"
    assert sender.calls == []


async def test_send_feishu_message_direct_user_id_success_saves_history(monkeypatch) -> None:
    target = _message_module()
    sender = _FeishuSender([{"code": 0, "msg": "ok"}])
    platform_calls, session_calls, message_inserts = _patch_dependencies(
        monkeypatch,
        target,
        config=_config(),
        direct_relationship=_relationship(),
        sender=sender,
    )

    result = await agent_tools._send_feishu_message(uuid.uuid4(), {"user_id": "ou_direct", "message": "hello"})

    assert result == "✅ 消息已发送（user_id: ou_direct）"
    assert sender.calls[0][2] == "ou_direct"
    assert sender.calls[0][5] == "user_id"
    assert len(platform_calls) == 1
    assert session_calls[0]["external_conv_id"] == "feishu_p2p_ou_direct"
    assert message_inserts[0]["content"] == "hello"
    assert message_inserts[0]["role"] == "assistant"


async def test_send_feishu_message_direct_user_id_requires_relationship(monkeypatch) -> None:
    target = _message_module()
    sender = _FeishuSender([])
    _patch_dependencies(monkeypatch, target, config=_config(), direct_relationship=None, sender=sender)

    result = await agent_tools._send_feishu_message(uuid.uuid4(), {"user_id": "ou_missing", "message": "hello"})

    assert result == "❌ Recipient is not in your active relationship network"
    assert sender.calls == []


async def test_send_feishu_message_direct_user_id_rejects_inactive_relationship(monkeypatch) -> None:
    target = _message_module()
    relationship = _relationship(status="inactive", reason="paused")
    sender = _FeishuSender([])
    _patch_dependencies(monkeypatch, target, config=_config(), direct_relationship=relationship, sender=sender)

    result = await agent_tools._send_feishu_message(uuid.uuid4(), {"user_id": "ou_inactive", "message": "hello"})

    assert result == "❌ Relationship to recipient is not active (paused)"
    assert sender.calls == []


async def test_send_feishu_message_named_member_success_saves_history(monkeypatch) -> None:
    target = _message_module()
    sender = _FeishuSender([{"code": 0, "msg": "ok"}])
    platform_calls, session_calls, message_inserts = _patch_dependencies(
        monkeypatch,
        target,
        config=_config(),
        named_relationships=[_relationship()],
        sender=sender,
    )

    result = await agent_tools._send_feishu_message(uuid.uuid4(), {"member_name": "Alice", "message": "hello"})

    assert result == "✅ Successfully sent message to Alice"
    assert sender.calls[0][2] == "ou_alice"
    assert len(platform_calls) == 1
    assert session_calls[0]["external_conv_id"] == "feishu_p2p_ou_alice"
    assert message_inserts[0]["content"] == "hello"


async def test_send_feishu_message_feishu_api_error_uses_user_message(monkeypatch) -> None:
    target = _message_module()
    error = FeishuAPIError(stage="send", code=99991663, msg="rate limited", troubleshooter="retry later")
    sender = _FeishuSender([error])
    _patch_dependencies(monkeypatch, target, config=_config(), direct_relationship=_relationship(), sender=sender)

    result = await agent_tools._send_feishu_message(uuid.uuid4(), {"user_id": "ou_direct", "message": "hello"})

    assert result == "❌ 飞书发送失败：rate limited (code 99991663)\nretry later"


async def test_send_feishu_message_provider_nonzero_responses_keep_existing_strings(monkeypatch) -> None:
    target = _message_module()
    sender = _FeishuSender([{"code": 19001, "msg": "bad receive id"}])
    _patch_dependencies(monkeypatch, target, config=_config(), direct_relationship=_relationship(), sender=sender)

    direct_result = await agent_tools._send_feishu_message(uuid.uuid4(), {"user_id": "ou_direct", "message": "hello"})

    assert direct_result == "❌ 发送失败：bad receive id (code 19001)"

    named_sender = _FeishuSender([{"code": 19002, "msg": "permission denied"}])
    _patch_dependencies(
        monkeypatch,
        target,
        config=_config(),
        named_relationships=[_relationship()],
        sender=named_sender,
    )

    named_result = await agent_tools._send_feishu_message(uuid.uuid4(), {"member_name": "Alice", "message": "hello"})

    assert named_result == "发送失败: permission denied (code 19002)"


async def test_send_feishu_registry_handler_uses_extracted_implementation(monkeypatch) -> None:
    from app.services.agent_tool_exec import registry

    target = _message_module()
    calls = []

    async def send_message(agent_id: uuid.UUID, arguments: registry.ToolArguments) -> str:
        calls.append((agent_id, arguments))
        return "sent through extracted module"

    monkeypatch.setattr(target, "_send_feishu_message", send_message)
    handler = registry.resolve("send_feishu_message")
    assert handler is not None
    agent_id = uuid.uuid4()
    arguments: registry.ToolArguments = {"member_name": "Alice", "message": "hello"}

    handler_result = handler(
        arguments=arguments,
        agent_id=agent_id,
        user_id=uuid.uuid4(),
        session_id="session-feishu",
        on_output=None,
    )
    result = handler_result if isinstance(handler_result, str) else await handler_result

    assert result == "sent through extracted module"
    assert calls == [(agent_id, arguments)]
