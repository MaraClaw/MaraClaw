from __future__ import annotations

import importlib
import uuid
from collections import deque
from types import SimpleNamespace
from typing import Literal

from app.core.json_types import JsonObject
from app.services import agent_tools
from app.services.agent_tool_exec import channel_context, registry
from app.services.agent_tool_exec.registry import ToolArgumentValue

type QueuedResponse = JsonObject | BaseException
type HttpCall = tuple[str, str, JsonObject]
type ApprovalCall = (
    tuple[Literal["create_approval_instance"], tuple[str, str, str, str, str]]
    | tuple[Literal["query_approval_instances"], tuple[str, str, str, str | None]]
    | tuple[Literal["get_approval_instance"], tuple[str, str, str]]
)


class _Response:
    def __init__(self, payload: JsonObject) -> None:
        self._payload = payload

    def json(self) -> JsonObject:
        return self._payload


class _FakeAsyncClient:
    def __init__(self, responses: deque[QueuedResponse], calls: list[HttpCall], timeout: int) -> None:
        self._responses = responses
        self._calls = calls
        self._timeout = timeout

    async def __aenter__(self) -> _FakeAsyncClient:
        return self

    async def __aexit__(self, *_args) -> bool:
        return False

    def _response(self) -> _Response:
        payload = self._responses.popleft()
        if isinstance(payload, BaseException):
            raise payload
        return _Response(payload)

    async def get(self, url: str, **kwargs: JsonObject) -> _Response:
        self._calls.append(("GET", url, {"timeout": self._timeout, **kwargs}))
        return self._response()

    async def post(self, url: str, **kwargs: JsonObject) -> _Response:
        self._calls.append(("POST", url, {"timeout": self._timeout, **kwargs}))
        return self._response()

    async def patch(self, url: str, **kwargs: JsonObject) -> _Response:
        self._calls.append(("PATCH", url, {"timeout": self._timeout, **kwargs}))
        return self._response()

    async def delete(self, url: str, **kwargs: JsonObject) -> _Response:
        self._calls.append(("DELETE", url, {"timeout": self._timeout, **kwargs}))
        return self._response()


class _FakeHttpxModule:
    def __init__(self, *responses: QueuedResponse) -> None:
        self.responses: deque[QueuedResponse] = deque(responses)
        self.calls: list[HttpCall] = []
        self.AsyncClient = lambda timeout: _FakeAsyncClient(self.responses, self.calls, timeout)


class _FakeFeishuService:
    def __init__(self, responses: dict[str, list[QueuedResponse]] | None = None) -> None:
        self._responses = {name: deque(values) for name, values in (responses or {}).items()}
        self.token_calls: list[tuple[str, str]] = []
        self.calls: list[ApprovalCall] = []

    async def get_tenant_access_token(self, app_id: str, app_secret: str) -> str:
        self.token_calls.append((app_id, app_secret))
        return "tenant-token"

    async def create_approval_instance(
        self,
        app_id: str,
        app_secret: str,
        approval_code: str,
        user_id: str,
        form_data: str,
    ) -> JsonObject:
        self.calls.append(("create_approval_instance", (app_id, app_secret, approval_code, user_id, form_data)))
        return self._next("create_approval_instance")

    async def query_approval_instances(
        self, app_id: str, app_secret: str, approval_code: str, status: str | None
    ) -> JsonObject:
        self.calls.append(("query_approval_instances", (app_id, app_secret, approval_code, status)))
        return self._next("query_approval_instances")

    async def get_approval_instance(self, app_id: str, app_secret: str, instance_id: str) -> JsonObject:
        self.calls.append(("get_approval_instance", (app_id, app_secret, instance_id)))
        return self._next("get_approval_instance")

    def _next(self, name: str) -> JsonObject:
        payload = self._responses[name].popleft()
        if isinstance(payload, BaseException):
            raise payload
        return payload


class _FakeResult:
    def __init__(self, *, scalar_value=None, scalars_list=()) -> None:
        self._scalar_value = scalar_value
        self._scalars_list = tuple(scalars_list)

    def scalar_one_or_none(self):
        return self._scalar_value

    def scalars(self):
        return SimpleNamespace(all=lambda: list(self._scalars_list))


class _FakeSession:
    def __init__(self, responses) -> None:
        self._responses = deque(responses)

    async def execute(self, _statement):
        response = self._responses.popleft()
        if isinstance(response, BaseException):
            raise response
        return response


class _FakeSessionFactory:
    def __init__(self, session: _FakeSession) -> None:
        self._session = session

    def __call__(self):
        return self

    async def __aenter__(self) -> _FakeSession:
        return self._session

    async def __aexit__(self, *_args) -> bool:
        return False


class _Field:
    def ilike(self, _pattern: str):
        return self


class _Query:
    def where(self, *_conditions):
        return self


class _AgentModel:
    id = _Field()
    status = _Field()
    tenant_id = _Field()


class _OrgMember:
    name = _Field()
    tenant_id = _Field()


class _UserModel:
    display_name = _Field()
    tenant_id = _Field()


TASK12_TOOL_NAMES = (
    "feishu_drive_share",
    "feishu_drive_delete",
    "feishu_user_search",
    "feishu_calendar_list",
    "feishu_calendar_create",
    "feishu_calendar_update",
    "feishu_calendar_delete",
    "feishu_approval_create",
    "feishu_approval_query",
    "feishu_approval_get",
)


def _drive_module():
    return importlib.import_module("app.services.agent_tool_exec._agent_tool_exec_feishu_drive")


def _calendar_module():
    return importlib.import_module("app.services.agent_tool_exec._agent_tool_exec_feishu_calendar")


def _approvals_module():
    return importlib.import_module("app.services.agent_tool_exec._agent_tool_exec_feishu_approvals")


def _contacts_module():
    return importlib.import_module("app.services.agent_tool_exec._agent_tool_exec_feishu_contacts")


def _patch_credentials(monkeypatch, service: _FakeFeishuService | None = None) -> None:
    async def get_credentials(_agent_id: uuid.UUID) -> tuple[str, str]:
        return "app-id", "app-secret"

    monkeypatch.setattr(agent_tools, "_get_feishu_credentials", get_credentials)
    if service is not None:
        monkeypatch.setattr(importlib.import_module("app.services.feishu_service"), "feishu_service", service)


async def test_drive_share_uses_facade_wiki_and_user_search_seams(monkeypatch) -> None:
    target = _drive_module()
    service = _FakeFeishuService()
    _patch_credentials(monkeypatch, service)
    seam_calls: list[tuple[str, ToolArgumentValue]] = []

    async def wiki_get_node(token: str, auth_token: str) -> JsonObject | None:
        seam_calls.append(("wiki", f"{token}:{auth_token}"))
        return None

    async def user_search(_agent_id: uuid.UUID, arguments: registry.ToolArguments) -> str:
        seam_calls.append(("search", arguments["name"]))
        return "🔍 找到匹配「Alice」的用户：\n\n• **Alice**\n  open_id: `ou_Alice123`"

    monkeypatch.setattr(agent_tools, "_feishu_wiki_get_node", wiki_get_node)
    monkeypatch.setattr(agent_tools, "_feishu_user_search", user_search)
    httpx = _FakeHttpxModule({"code": 0, "data": {}})
    monkeypatch.setattr(target, "_httpx_module", lambda: httpx)

    result = await target._feishu_drive_share(
        uuid.uuid4(),
        {"document_token": "doc-token", "action": "add", "member_names": ["Alice"], "permission": "view"},
    )

    assert result == "✅ 已将「Alice」添加为**view**权限协作者"
    assert seam_calls == [("wiki", "doc-token:tenant-token"), ("search", "Alice")]
    assert httpx.calls[0][0] == "POST"
    assert httpx.calls[0][2]["json"] == {"member_type": "openid", "member_id": "ou_Alice123", "perm": "view"}


async def test_drive_delete_preserves_async_success_and_permission_error(monkeypatch) -> None:
    target = _drive_module()
    _patch_credentials(monkeypatch, _FakeFeishuService())
    success_httpx = _FakeHttpxModule({"code": 0, "data": {"task_id": "task-1"}})
    monkeypatch.setattr(target, "_httpx_module", lambda: success_httpx)

    success = await target._feishu_drive_delete(uuid.uuid4(), {"file_token": "fld-token", "file_type": "folder"})

    assert (
        success
        == "✅ 已提交文件夹删除任务（异步执行中）。\n📋 任务 ID: `task-1`\n文件夹删除为异步操作，文件会被移至回收站。"
    )

    error_httpx = _FakeHttpxModule({"code": 1061004, "msg": "no permission"})
    monkeypatch.setattr(target, "_httpx_module", lambda: error_httpx)

    error = await target._feishu_drive_delete(uuid.uuid4(), {"file_token": "doc-token", "file_type": "docx"})

    assert error == (
        "❌ 权限不足（code 1061004）\n"
        "需要满足以下条件之一：\n"
        "• 文件所有者 + 父文件夹编辑权限\n"
        "• 父文件夹的所有者或 full_access 权限\n"
        "同时需要在飞书开放平台开通：drive:drive 或 space:document:delete"
    )


async def test_calendar_create_auto_invites_sender_and_resolves_names_through_facade(monkeypatch) -> None:
    target = _calendar_module()
    _patch_credentials(monkeypatch, _FakeFeishuService())
    resolved_names: list[ToolArgumentValue] = []

    async def get_calendar_id(_token: str) -> tuple[str, str | None]:
        return "cal-1", None

    async def resolve_open_id(_token: str, email: str) -> str | None:
        return "ou_email" if email == "owner@example.test" else None

    async def user_search(_agent_id: uuid.UUID, arguments: registry.ToolArguments) -> str:
        resolved_names.append(arguments["name"])
        return "open_id: `ou_Alice123`"

    monkeypatch.setattr(agent_tools, "_get_agent_calendar_id", get_calendar_id)
    monkeypatch.setattr(agent_tools, "_feishu_resolve_open_id", resolve_open_id)
    monkeypatch.setattr(agent_tools, "_iso_to_ts", lambda value: 1_700_000_000.0 if "09" in value else 1_700_003_600.0)
    monkeypatch.setattr(agent_tools, "_feishu_user_search", user_search)
    httpx = _FakeHttpxModule(
        {"code": 0, "data": {"event": {"event_id": "event-1"}}},
        {"code": 0},
        {"code": 0},
        {"code": 0},
    )
    monkeypatch.setattr(target, "_httpx_module", lambda: httpx)
    token = channel_context.channel_feishu_sender_open_id.set("ou_sender")
    try:
        result = await target._feishu_calendar_create(
            uuid.uuid4(),
            {
                "summary": "Planning",
                "start_time": "2026-07-07T09:00:00+08:00",
                "end_time": "2026-07-07T10:00:00+08:00",
                "user_email": "owner@example.test",
                "attendee_names": ["Alice"],
            },
        )
    finally:
        channel_context.channel_feishu_sender_open_id.reset(token)

    assert result == (
        "✅ 日历事件已创建！\n"
        "**标题**: Planning\n"
        "**时间**: 2026-07-07T09:00:00+08:00 → 2026-07-07T10:00:00+08:00\n"
        "**参与人**: Alice, owner@example.test\n"
        "**Event ID**: `event-1`\n"
        "（已向您发送日历邀请，请在飞书日历中确认）"
    )
    assert resolved_names == ["Alice"]
    attendee_payloads = [call[2]["json"] for call in httpx.calls if call[1].endswith("/attendees")]
    assert attendee_payloads == [
        {"attendees": [{"type": "user", "user_id": "ou_Alice123"}]},
        {"attendees": [{"type": "user", "user_id": "ou_email"}]},
        {"attendees": [{"type": "user", "user_id": "ou_sender"}]},
    ]


async def test_calendar_update_preserves_provider_error_shape(monkeypatch) -> None:
    target = _calendar_module()
    _patch_credentials(monkeypatch, _FakeFeishuService())

    async def resolve_open_id(_token: str, _email: str) -> str:
        return "ou_user"

    async def get_calendar_id(_token: str) -> tuple[str, None]:
        return "cal-1", None

    monkeypatch.setattr(agent_tools, "_feishu_resolve_open_id", resolve_open_id)
    monkeypatch.setattr(agent_tools, "_get_agent_calendar_id", get_calendar_id)
    httpx = _FakeHttpxModule({"code": 19000, "msg": "calendar denied"})
    monkeypatch.setattr(target, "_httpx_module", lambda: httpx)

    result = await target._feishu_calendar_update(
        uuid.uuid4(),
        {"user_email": "owner@example.test", "event_id": "event-1", "summary": "New title"},
    )

    assert result == "❌ Failed to update: calendar denied (code 19000)"


async def test_approval_handlers_preserve_check_err_mapping_success_and_exception_shape(monkeypatch) -> None:
    target = _approvals_module()
    service = _FakeFeishuService(
        {
            "create_approval_instance": [{"code": 0, "data": {"instance_code": "approval-1"}}],
            "query_approval_instances": [{"code": 19000, "msg": "provider denied"}],
            "get_approval_instance": [RuntimeError("x" * 350)],
        }
    )
    _patch_credentials(monkeypatch, service)
    monkeypatch.setattr(
        agent_tools, "_check_feishu_err", lambda resp: "mapped provider error" if resp.get("code") != 0 else None
    )

    created = await target._feishu_approval_create(
        uuid.uuid4(),
        {"approval_code": "code-1", "user_id": "ou_user", "form_data": "{}"},
    )
    mapped = await target._feishu_approval_query(uuid.uuid4(), {"approval_code": "code-1", "status": "PENDING"})
    failed = await target._feishu_approval_get(uuid.uuid4(), {"instance_id": "approval-1"})

    assert created == "✅ 审批发起成功！\n审批实例 ID: `approval-1`"
    assert mapped == "mapped provider error"
    assert failed == f"Failed: {'x' * 300}"


async def test_user_search_uses_facade_db_seams_and_contact_refresh_deletes_cache(monkeypatch, tmp_path) -> None:
    from app.dao.agent_dao import agent_dao
    from app.dao.org_member_dao import org_member_dao

    target = _contacts_module()
    member = SimpleNamespace(
        name="Alice",
        external_id="user-alice",
        open_id="ou_Alice123",
        email="alice@example.test",
        department_path="Engineering",
    )
    _patch_credentials(monkeypatch)

    async def fake_get_agent(_agent_id):
        return SimpleNamespace(tenant_id=uuid.uuid4())

    async def fake_list_active_filtered(**_kwargs):
        return [(member, "feishu", "feishu")]

    monkeypatch.setattr(agent_dao, "get", fake_get_agent)
    monkeypatch.setattr(org_member_dao, "list_active_filtered", fake_list_active_filtered)

    result = await target._feishu_user_search(uuid.uuid4(), {"name": "Alice"})

    assert result == (
        "🔍 从通讯录找到 1 位匹配「Alice」的用户：\n\n"
        "• **Alice**\n"
        "  user_id: `user-alice`\n"
        "  open_id: `ou_Alice123`\n"
        "  邮箱: alice@example.test\n"
        "  部门: Engineering"
    )

    cache_file = tmp_path / "feishu_contacts_cache.json"
    cache_file.write_text("{}")
    monkeypatch.setattr(target, "_contacts_cache_file", lambda _agent_id: cache_file)

    await target._feishu_contacts_refresh(uuid.uuid4())

    assert not cache_file.exists()


async def test_user_search_db_miss_returns_empty_cache_message_without_facade_cache(monkeypatch) -> None:
    from app.dao.agent_dao import agent_dao
    from app.dao.org_member_dao import org_member_dao

    target = _contacts_module()
    _patch_credentials(monkeypatch)
    monkeypatch.delattr(agent_tools, "_cached_users", raising=False)
    monkeypatch.setattr(target, "_cached_users", [])

    async def fake_get_agent(_agent_id):
        return SimpleNamespace(tenant_id=uuid.uuid4())

    async def fake_list_active_filtered(**_kwargs):
        raise RuntimeError("db unavailable")

    monkeypatch.setattr(agent_dao, "get", fake_get_agent)
    monkeypatch.setattr(org_member_dao, "list_active_filtered", fake_list_active_filtered)

    result = await target._feishu_user_search(uuid.uuid4(), {"name": "Bob"})

    assert result == (
        "❌ 本地通讯录缓存为空，暂时无法搜索「Bob」。\n\n"
        "通讯录缓存会在同事向机器人发消息时自动建立。\n"
        "如果「覃睿」从未给机器人发过消息，可以请他先给机器人发一条消息，"
        "之后就能直接搜索到他了。\n\n"
        "或者，请直接告诉我「覃睿」的飞书 open_id 或邮箱，我可以立刻操作。"
    )


async def test_user_search_db_miss_returns_cache_miss_message_with_module_cache(monkeypatch) -> None:
    from app.dao.agent_dao import agent_dao
    from app.dao.org_member_dao import org_member_dao

    target = _contacts_module()
    _patch_credentials(monkeypatch)
    monkeypatch.delattr(agent_tools, "_cached_users", raising=False)
    monkeypatch.setattr(target, "_cached_users", [{"name": "Alice"}, {"name": "Carol"}])

    async def fake_get_agent(_agent_id):
        return SimpleNamespace(tenant_id=uuid.uuid4())

    async def fake_list_active_filtered(**_kwargs):
        raise RuntimeError("db unavailable")

    monkeypatch.setattr(agent_dao, "get", fake_get_agent)
    monkeypatch.setattr(org_member_dao, "list_active_filtered", fake_list_active_filtered)

    result = await target._feishu_user_search(uuid.uuid4(), {"name": "Bob"})

    assert result == (
        "❌ 未在本地通讯录（已缓存 2 人）中找到「Bob」。\n\n"
        "通讯录缓存来自给机器人发过消息的同事。\n"
        "如果「{name}」从未给机器人发消息，请他先发一条，之后即可自动识别。\n"
        "或者请直接提供其飞书 open_id / 工作邮箱。"
    )


async def test_task12_registry_handlers_call_extracted_modules_not_facade(monkeypatch) -> None:
    drive = _drive_module()
    contacts = _contacts_module()
    approvals = _approvals_module()
    calls: list[tuple[str, uuid.UUID, registry.ToolArguments]] = []

    async def extracted_drive(agent_id: uuid.UUID, arguments: registry.ToolArguments) -> str:
        calls.append(("drive", agent_id, arguments))
        return "drive extracted"

    async def extracted_contacts(agent_id: uuid.UUID, arguments: registry.ToolArguments) -> str:
        calls.append(("contacts", agent_id, arguments))
        return "contacts extracted"

    async def extracted_approval(agent_id: uuid.UUID, arguments: registry.ToolArguments) -> str:
        calls.append(("approval", agent_id, arguments))
        return "approval extracted"

    async def legacy_fail(_agent_id: uuid.UUID, _arguments: registry.ToolArguments) -> str:
        raise AssertionError("Task 12 registry handler must not defer through agent_tools facade")

    monkeypatch.setattr(drive, "_feishu_drive_share", extracted_drive)
    monkeypatch.setattr(contacts, "_feishu_user_search", extracted_contacts)
    monkeypatch.setattr(approvals, "_feishu_approval_get", extracted_approval)
    monkeypatch.setattr(agent_tools, "_feishu_drive_share", legacy_fail)
    monkeypatch.setattr(agent_tools, "_feishu_user_search", legacy_fail)
    monkeypatch.setattr(agent_tools, "_feishu_approval_get", legacy_fail)

    for tool_name in TASK12_TOOL_NAMES:
        assert registry.resolve(tool_name) is not None

    agent_id = uuid.uuid4()
    drive_handler = registry.resolve("feishu_drive_share")
    contact_handler = registry.resolve("feishu_user_search")
    approval_handler = registry.resolve("feishu_approval_get")
    assert drive_handler is not None
    assert contact_handler is not None
    assert approval_handler is not None

    drive_result = drive_handler(
        arguments={"document_token": "doc"},
        agent_id=agent_id,
        user_id=uuid.uuid4(),
        session_id="session-task12",
        on_output=None,
    )
    contact_result = contact_handler(
        arguments={"name": "Alice"},
        agent_id=agent_id,
        user_id=uuid.uuid4(),
        session_id="session-task12",
        on_output=None,
    )
    approval_result = approval_handler(
        arguments={"instance_id": "approval-1"},
        agent_id=agent_id,
        user_id=uuid.uuid4(),
        session_id="session-task12",
        on_output=None,
    )

    assert (drive_result if isinstance(drive_result, str) else await drive_result) == "drive extracted"
    assert (contact_result if isinstance(contact_result, str) else await contact_result) == "contacts extracted"
    assert (approval_result if isinstance(approval_result, str) else await approval_result) == "approval extracted"
    assert calls == [
        ("drive", agent_id, {"document_token": "doc"}),
        ("contacts", agent_id, {"name": "Alice"}),
        ("approval", agent_id, {"instance_id": "approval-1"}),
    ]


async def test_facade_wrappers_call_extracted_modules(monkeypatch) -> None:
    approvals = _approvals_module()
    contacts = _contacts_module()
    seen: list[tuple[str, registry.ToolArguments]] = []

    async def approval_get(_agent_id: uuid.UUID, arguments: registry.ToolArguments) -> str:
        seen.append(("approval", arguments))
        return "approval via facade wrapper"

    async def user_search(_agent_id: uuid.UUID, arguments: registry.ToolArguments) -> str:
        seen.append(("user", arguments))
        return "user via facade wrapper"

    monkeypatch.setattr(approvals, "_feishu_approval_get", approval_get)
    monkeypatch.setattr(contacts, "_feishu_user_search", user_search)

    approval_result = await agent_tools._feishu_approval_get(uuid.uuid4(), {"instance_id": "approval-1"})
    user_result = await agent_tools._feishu_user_search(uuid.uuid4(), {"name": "Alice"})

    assert approval_result == "approval via facade wrapper"
    assert user_result == "user via facade wrapper"
    assert seen == [("approval", {"instance_id": "approval-1"}), ("user", {"name": "Alice"})]
