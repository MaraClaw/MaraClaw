import importlib
import inspect
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services import agent_tools
from app.services.agent_tool_exec import channel_context, channel_messaging, dispatcher, registry


def test_okr_daily_collection_uses_platform_messaging_owner():
    owner = importlib.import_module("app.services.agent_tool_exec.platform_messaging")
    okr_daily_collection = importlib.import_module("app.services.okr_daily_collection")

    # No facade re-export: OKR imports the owner module directly.
    assert okr_daily_collection._send_platform_message is owner._send_platform_message


async def test_platform_facade_defers_once_and_preserves_arguments_identity(monkeypatch):
    owner = importlib.import_module("app.services.agent_tool_exec.platform_messaging")
    calls = []

    async def send(agent_id, arguments):
        calls.append((agent_id, arguments))
        return "sent"

    monkeypatch.setattr(owner, "_send_platform_message", send)
    agent_id = uuid.uuid4()
    arguments = {"username": "Alex", "message": "hello"}

    assert await agent_tools._send_platform_message(agent_id, arguments) == "sent"
    assert calls == [(agent_id, arguments)]
    assert calls[0][1] is arguments


async def test_channel_platform_user_reroutes_once_through_the_facade(monkeypatch):
    member = SimpleNamespace(
        name="Alex",
        status="active",
        user_id=uuid.uuid4(),
        external_id=None,
        open_id=None,
    )
    relationship = SimpleNamespace(
        member=member,
        provider_type=None,
        agent_id=uuid.uuid4(),
        member_id=uuid.uuid4(),
    )
    platform_user = SimpleNamespace(display_name="Alex", email="alex@example.com", identity=None)
    calls = []

    async def list_rels(_agent_id):
        return [relationship]

    async def status(*_args, **_kwargs):
        return {"access_status": "active"}

    async def get_user(_user_id):
        return platform_user

    async def platform_send(agent_id, arguments):
        calls.append((agent_id, arguments))
        return "rerouted"

    facade = SimpleNamespace(
        _send_platform_message=platform_send,
        logger=SimpleNamespace(
            info=lambda *_args, **_kwargs: None,
            warning=lambda *_args, **_kwargs: None,
            exception=lambda *_args, **_kwargs: None,
        ),
    )
    monkeypatch.setattr(channel_messaging, "agent_tools", facade)
    monkeypatch.setattr(
        channel_messaging.agent_relationship_dao,
        "list_for_agent_with_members_and_providers",
        list_rels,
    )
    monkeypatch.setattr(channel_messaging, "evaluate_human_relationship_status", status)
    monkeypatch.setattr(channel_messaging.user_dao, "get_with_identity", get_user)

    assert (
        await channel_messaging._send_channel_message(uuid.uuid4(), {"member_name": "Alex", "message": " hello "})
        == "rerouted"
    )
    assert len(calls) == 1
    assert calls[0][1] == {"username": "alex@example.com", "message": "hello"}


@pytest.mark.parametrize("name", ["send_platform_message", "send_channel_message", "send_channel_file"])
async def test_dispatcher_routes_messages_and_files_through_registered_handlers(monkeypatch, tmp_path, name):
    # Given: registered messaging handlers and a tenant-scoped workspace.
    from app.dao import agent_dao
    from app.services import activity_logger
    from app.services.agent_tool_exec import channel_files

    agent_id = uuid.uuid4()
    arguments: registry.ToolArguments = {"file_path": "report.txt", "message": "ready"}
    calls = []
    workspaces = []

    async def tenant_lookup(_agent_id):
        return "tenant-im"

    async def no_agent(_agent_id):
        return None

    async def send(*args):
        calls.append(args)
        return "sent through registry"

    async def workspace(_agent_id, tenant_id, callback, *, paths, sync_back=False):
        workspaces.append((_agent_id, tenant_id, paths, sync_back))
        return await callback(tmp_path)

    async def log(*_args, **_kwargs):
        return None

    monkeypatch.setattr(agent_tools, "_get_agent_tenant_id", tenant_lookup)
    monkeypatch.setattr(agent_tools, "_agent_workspace_root", lambda _: tmp_path)
    monkeypatch.setattr(agent_dao, "get", no_agent)
    owner = channel_files if name == "send_channel_file" else agent_tools
    monkeypatch.setattr(owner, f"_{name}", send)
    monkeypatch.setattr(agent_tools, "_run_with_temp_workspace", workspace)
    monkeypatch.setattr(activity_logger, "log_activity", log)
    # When: the public dispatcher executes the tool normally.
    result = await dispatcher.execute_tool(name, arguments, agent_id, uuid.uuid4())
    # Then: each registered route delegates once with the original arguments.
    assert result == "sent through registry"
    if name == "send_channel_file":
        assert calls == [(agent_id, tmp_path, arguments)]
        assert workspaces == [(agent_id, "tenant-im", ["report.txt"], False)]
    else:
        assert calls == [(agent_id, arguments)]
        assert workspaces == []


async def test_channel_file_uses_the_facade_context_callback(tmp_path: Path):
    channel_files = importlib.import_module("app.services.agent_tool_exec.channel_files")
    source = tmp_path / "report.txt"
    source.write_text("report", encoding="utf-8")
    calls = []

    async def sender(path, message=""):
        calls.append((path, message))

    token = channel_context.channel_file_sender.set(sender)
    try:
        result = await channel_files._send_channel_file(
            uuid.uuid4(), tmp_path, {"file_path": "report.txt", "message": "ready"}
        )
    finally:
        channel_context.channel_file_sender.reset(token)

    assert result == "File 'report.txt' sent to user via channel."
    assert calls == [(source, "ready")]


async def test_feishu_attendees_keep_order_and_one_lookup_per_value_without_output(monkeypatch):
    support = importlib.import_module("app.services.agent_tool_exec.feishu_calendar_support")
    calls = []

    async def search(_agent_id, arguments):
        calls.append(("name", arguments["name"]))
        return "open_id: `ou_alice`"

    async def resolve(_token, email):
        calls.append(("email", email))
        return f"ou_{email.split('@')[0]}"

    facade = SimpleNamespace(
        _feishu_user_search=search,
        _feishu_resolve_open_id=resolve,
        channel_feishu_sender_open_id=channel_context.channel_feishu_sender_open_id,
        logger=SimpleNamespace(warning=lambda *_args: None),
    )
    monkeypatch.setattr(support, "agent_tools", facade)
    token = channel_context.channel_feishu_sender_open_id.set("ou_sender")
    try:
        open_ids, display = await support._calendar_attendees(
            uuid.uuid4(),
            {"attendee_names": ["Alice"], "attendee_emails": ["extra@example.test"]},
            "token",
            "owner@example.test",
        )
    finally:
        channel_context.channel_feishu_sender_open_id.reset(token)

    assert open_ids == ["ou_alice", "ou_extra", "ou_owner", "ou_sender"]
    assert display == ["Alice", "extra@example.test", "owner@example.test"]
    assert calls == [("name", "Alice"), ("email", "extra@example.test"), ("email", "owner@example.test")]
    assert "on_output" not in inspect.getsource(support._calendar_attendees)
