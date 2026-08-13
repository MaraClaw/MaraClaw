from __future__ import annotations

import secrets
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.dao.agent_dao import agent_dao
from app.dao.trigger_dao import agent_trigger_dao
from app.services import agent_tools, audit_logger, platform_service as platform_module
from app.services.agent_tool_exec import (
    _agent_tool_exec_triggers as triggers,
    registry,
)


def _agent(*, max_triggers: int | None = 20):
    return SimpleNamespace(max_triggers=max_triggers)


def _trigger(
    agent_id: uuid.UUID,
    *,
    name: str = "daily",
    type_: str = "interval",
    config=None,
    reason: str = "old reason",
    is_enabled: bool = True,
    fire_count: int = 0,
    max_fires: int | None = None,
    expires_at=None,
):
    return SimpleNamespace(
        agent_id=agent_id,
        name=name,
        type=type_,
        config=config or {"minutes": 5},
        reason=reason,
        focus_ref="focus-daily",
        is_enabled=is_enabled,
        fire_count=fire_count,
        max_fires=max_fires,
        expires_at=expires_at,
    )


async def _ensure_focus_item(_agent_id, *, focus_ref, description, system):
    return focus_ref or f"focus-{description}"


def _set_args(*, name: str = "daily", type_: str = "interval", config=None, reason: str = "check metrics"):
    trigger_config = {"minutes": 30} if config is None else config
    return {"name": name, "type": type_, "config": trigger_config, "reason": reason}


def _install_dao_defaults(monkeypatch: pytest.MonkeyPatch, *, agent=None, existing=None, count=0, created=None):
    """Patch agent_dao / agent_trigger_dao for trigger tool tests."""
    created_holder: list = []

    async def get_agent(_id):
        return agent if agent is not None else _agent()

    async def count_enabled(_agent_id):
        return count

    async def get_by_name(_agent_id, _name):
        return existing

    async def create(*, obj_in):
        row = SimpleNamespace(**obj_in)
        if not hasattr(row, "fire_count"):
            row.fire_count = 0
        if not hasattr(row, "is_enabled"):
            row.is_enabled = True
        if not hasattr(row, "max_fires"):
            row.max_fires = obj_in.get("max_fires")
        if not hasattr(row, "expires_at"):
            row.expires_at = obj_in.get("expires_at")
        created_holder.append(row)
        if created is not None:
            return created
        return row

    async def update(*, db_obj, obj_in):
        for key, value in obj_in.items():
            setattr(db_obj, key, value)
        return db_obj

    async def list_for_agent(_agent_id):
        return []

    monkeypatch.setattr(agent_dao, "get", get_agent)
    monkeypatch.setattr(agent_trigger_dao, "count_enabled_for_agent", count_enabled)
    monkeypatch.setattr(agent_trigger_dao, "get_by_agent_and_name", get_by_name)
    monkeypatch.setattr(agent_trigger_dao, "create", create)
    monkeypatch.setattr(agent_trigger_dao, "update", update)
    monkeypatch.setattr(agent_trigger_dao, "list_for_agent", list_for_agent)
    # Module-level aliases imported into triggers
    monkeypatch.setattr(triggers.agent_dao, "get", get_agent)
    monkeypatch.setattr(triggers.agent_trigger_dao, "count_enabled_for_agent", count_enabled)
    monkeypatch.setattr(triggers.agent_trigger_dao, "get_by_agent_and_name", get_by_name)
    monkeypatch.setattr(triggers.agent_trigger_dao, "create", create)
    monkeypatch.setattr(triggers.agent_trigger_dao, "update", update)
    monkeypatch.setattr(triggers.agent_trigger_dao, "list_for_agent", list_for_agent)
    return created_holder


@pytest.mark.asyncio
async def test_set_trigger_creates_trigger_and_links_focus(monkeypatch: pytest.MonkeyPatch):
    agent_id = uuid.uuid4()
    created_holder = _install_dao_defaults(monkeypatch)
    focus_calls = []

    async def ensure_focus_item(agent_id_arg, *, focus_ref, description, system):
        focus_calls.append((agent_id_arg, focus_ref, description, system))
        return "focus-linked"

    audit_log = AsyncMock()
    monkeypatch.setattr(triggers, "ensure_focus_item", ensure_focus_item)
    monkeypatch.setattr(audit_logger, "write_audit_log", audit_log)

    result = await agent_tools._handle_set_trigger(
        agent_id,
        _set_args(config={"minutes": 15}) | {"focus_ref": "focus-input"},
    )

    assert (
        result
        == "✅ Trigger 'daily' created (interval). It will fire according to your config and wake you up with the reason as context."
    )
    assert focus_calls == [(agent_id, "focus-input", "check metrics", False)]
    assert len(created_holder) == 1
    trigger = created_holder[0]
    assert trigger.name == "daily"
    assert trigger.type == "interval"
    assert trigger.config == {"minutes": 15}
    assert trigger.reason == "check metrics"
    assert trigger.focus_ref == "focus-linked"
    audit_log.assert_awaited_once()


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        (
            {"type": "once", "config": {"at": "2026-03-10T09:00:00+08:00"}, "reason": "wake"},
            "❌ Missing required argument 'name'",
        ),
        (
            {"name": "once", "type": "once", "config": {"at": "2026-03-10T09:00:00+08:00"}},
            "❌ Missing required argument 'reason'",
        ),
        (_set_args(type_="cron", config={}), '❌ cron trigger requires config.expr, e.g. {"expr": "0 9 * * *"}'),
        (_set_args(type_="cron", config={"expr": "not cron"}), "❌ Invalid cron expression: 'not cron'"),
        (
            _set_args(type_="once", config={}),
            '❌ once trigger requires config.at, e.g. {"at": "2026-03-10T09:00:00+08:00"}',
        ),
        (_set_args(type_="interval", config={}), '❌ interval trigger requires config.minutes, e.g. {"minutes": 30}'),
        (_set_args(type_="poll", config={}), "❌ poll trigger requires config.url"),
        (
            _set_args(type_="on_message", config={}),
            "❌ on_message trigger requires config.from_agent_name (for agents) or config.from_user_name (for human users on Feishu/Slack/Discord)",
        ),
    ],
)
async def test_set_trigger_validation_strings(monkeypatch: pytest.MonkeyPatch, arguments, expected):
    monkeypatch.setattr(triggers, "ensure_focus_item", _ensure_focus_item)

    result = await agent_tools._handle_set_trigger(uuid.uuid4(), arguments)

    assert result == expected


@pytest.mark.asyncio
async def test_set_trigger_invalid_type_lists_valid_types(monkeypatch: pytest.MonkeyPatch):
    result = await agent_tools._handle_set_trigger(uuid.uuid4(), _set_args(type_="bogus"))

    prefix, valid_types = result.split("Valid types: ")
    assert prefix == "❌ Invalid trigger type 'bogus'. "
    assert set(valid_types.split(", ")) == {"cron", "once", "interval", "poll", "on_message", "webhook"}


@pytest.mark.asyncio
async def test_on_message_set_trigger_snapshots_latest_message_timestamp(monkeypatch: pytest.MonkeyPatch):
    agent_id = uuid.uuid4()
    latest = datetime(2026, 3, 10, 9, 0, tzinfo=UTC)
    created_holder = _install_dao_defaults(monkeypatch)
    monkeypatch.setattr(triggers, "ensure_focus_item", _ensure_focus_item)
    monkeypatch.setattr(audit_logger, "write_audit_log", AsyncMock())

    async def snapshot(_facade, _agent_id, config):
        config["_since_ts"] = latest.isoformat()

    monkeypatch.setattr(triggers._TRIGGER_HELPERS, "_snapshot_latest_message", snapshot)

    result = await agent_tools._handle_set_trigger(
        agent_id,
        _set_args(type_="on_message", config={"from_agent_name": "Bob"}),
    )

    assert (
        result
        == "✅ Trigger 'daily' created (on_message). It will fire according to your config and wake you up with the reason as context."
    )
    trigger = created_holder[0]
    assert trigger.config["_since_ts"] == latest.isoformat()
    assert trigger.max_fires == 100
    assert trigger.expires_at is not None


@pytest.mark.asyncio
async def test_on_message_set_trigger_keeps_fallback_when_snapshot_fails(monkeypatch: pytest.MonkeyPatch):
    created_holder = _install_dao_defaults(monkeypatch)
    monkeypatch.setattr(triggers, "ensure_focus_item", _ensure_focus_item)
    monkeypatch.setattr(audit_logger, "write_audit_log", AsyncMock())

    async def snapshot(_facade, _agent_id, config):
        # Real helper swallows DB errors; model that as a no-op with no _since_ts.
        return

    monkeypatch.setattr(triggers._TRIGGER_HELPERS, "_snapshot_latest_message", snapshot)

    result = await agent_tools._handle_set_trigger(
        uuid.uuid4(),
        _set_args(type_="on_message", config={"from_user_name": "Ray"}),
    )

    assert (
        result
        == "✅ Trigger 'daily' created (on_message). It will fire according to your config and wake you up with the reason as context."
    )
    assert "_since_ts" not in created_holder[0].config


@pytest.mark.asyncio
async def test_webhook_set_trigger_adds_token_and_returns_url(monkeypatch: pytest.MonkeyPatch):
    created_holder = _install_dao_defaults(monkeypatch)

    async def public_base_url() -> str:
        return "https://agents.example/"

    monkeypatch.setattr(triggers, "ensure_focus_item", _ensure_focus_item)
    monkeypatch.setattr(audit_logger, "write_audit_log", AsyncMock())
    monkeypatch.setattr(secrets, "token_urlsafe", lambda _length: "token123")
    monkeypatch.setattr(platform_module.platform_service, "get_public_base_url", public_base_url)

    result = await agent_tools._handle_set_trigger(uuid.uuid4(), _set_args(name="hook", type_="webhook", config={}))

    assert created_holder[0].config["token"] == "token123"
    assert result.startswith(
        "✅ Webhook trigger 'hook' created.\n\nWebhook URL: https://agents.example/api/webhooks/t/token123"
    )
    assert "Tell the user to configure this URL" in result


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        ({}, "❌ Missing required argument 'name'"),
        ({"name": "daily"}, "❌ Provide at least one of 'config' or 'reason' to update"),
    ],
)
async def test_update_trigger_validates_request_shape(arguments, expected):
    result = await agent_tools._handle_update_trigger(uuid.uuid4(), arguments)

    assert result == expected


@pytest.mark.asyncio
async def test_update_trigger_not_found_and_happy_response_shapes(monkeypatch: pytest.MonkeyPatch):
    agent_id = uuid.uuid4()
    _install_dao_defaults(monkeypatch, existing=None)
    assert (
        await agent_tools._handle_update_trigger(agent_id, {"name": "missing", "reason": "new"})
        == "❌ Trigger 'missing' not found"
    )

    trigger = _trigger(agent_id)
    _install_dao_defaults(monkeypatch, existing=trigger)
    audit_log = AsyncMock()
    monkeypatch.setattr(audit_logger, "write_audit_log", audit_log)
    result = await agent_tools._handle_update_trigger(
        agent_id, {"name": "daily", "config": {"minutes": 10}, "reason": "new reason"}
    )

    assert result == "✅ Trigger 'daily' updated: config: {'minutes': 5} → {'minutes': 10}; reason updated"
    assert trigger.config == {"minutes": 10}
    assert trigger.reason == "new reason"
    audit_log.assert_awaited_once()


@pytest.mark.parametrize(
    ("db_trigger", "expected"),
    [
        (None, "❌ Trigger 'daily' not found"),
        (_trigger(uuid.uuid4(), is_enabled=False), "\u2139\ufe0f Trigger 'daily' is already disabled"),
    ],
)
async def test_cancel_trigger_not_found_and_already_disabled(monkeypatch: pytest.MonkeyPatch, db_trigger, expected):
    _install_dao_defaults(monkeypatch, existing=db_trigger)

    result = await agent_tools._handle_cancel_trigger(uuid.uuid4(), {"name": "daily"})

    assert result == expected


@pytest.mark.asyncio
async def test_cancel_trigger_missing_name_and_happy_response_shapes(monkeypatch: pytest.MonkeyPatch):
    assert await agent_tools._handle_cancel_trigger(uuid.uuid4(), {}) == "❌ Missing required argument 'name'"

    agent_id = uuid.uuid4()
    trigger = _trigger(agent_id)
    _install_dao_defaults(monkeypatch, existing=trigger)
    audit_log = AsyncMock()
    monkeypatch.setattr(audit_logger, "write_audit_log", audit_log)

    result = await agent_tools._handle_cancel_trigger(agent_id, {"name": "daily"})

    assert result == "✅ Trigger 'daily' cancelled. It will no longer fire."
    assert trigger.is_enabled is False
    audit_log.assert_awaited_once()


@pytest.mark.asyncio
async def test_list_triggers_empty_and_non_empty_markdown(monkeypatch: pytest.MonkeyPatch):
    agent_id = uuid.uuid4()
    _install_dao_defaults(monkeypatch)
    assert await agent_tools._handle_list_triggers(agent_id) == "No triggers found. Use set_trigger to create one."

    active = _trigger(
        agent_id, name="daily", type_="interval", config={"minutes": 5}, reason="check metrics", fire_count=2
    )
    disabled = _trigger(
        agent_id, name="hook", type_="webhook", config={"token": "tok"}, reason="ship", is_enabled=False
    )

    async def list_for_agent(_agent_id):
        return [active, disabled]

    monkeypatch.setattr(triggers.agent_trigger_dao, "list_for_agent", list_for_agent)

    result = await agent_tools._handle_list_triggers(agent_id)

    assert result.splitlines() == [
        "| Name | Type | Config | Reason | Status | Fires |",
        "|------|------|--------|--------|--------|-------|",
        "| daily | interval | {'minutes': 5} | check metrics | ✅ active | 2 |",
        "| hook | webhook | {'token': 'tok'} | ship | ⏸ disabled | 0 |",
    ]


async def test_registered_trigger_callbacks_use_local_owners(monkeypatch: pytest.MonkeyPatch) -> None:
    agent_id = uuid.uuid4()
    user_id = uuid.uuid4()
    calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = []

    async def set_owner(
        received_agent_id: uuid.UUID,
        arguments: registry.ToolArguments,
        *,
        session_id: str,
        user_id: uuid.UUID,
    ) -> str:
        calls.append(("set", (received_agent_id, arguments), {"session_id": session_id, "user_id": user_id}))
        return "set result"

    async def update_owner(received_agent_id: uuid.UUID, arguments: registry.ToolArguments) -> str:
        calls.append(("update", (received_agent_id, arguments), {}))
        return "update result"

    async def cancel_owner(received_agent_id: uuid.UUID, arguments: registry.ToolArguments) -> str:
        calls.append(("cancel", (received_agent_id, arguments), {}))
        return "cancel result"

    async def list_owner(received_agent_id: uuid.UUID) -> str:
        calls.append(("list", (received_agent_id,), {}))
        return "list result"

    async def legacy_owner(*_args: object, **_kwargs: object) -> str:
        raise AssertionError("registered trigger callbacks must not call legacy facade helpers")

    monkeypatch.setattr(triggers, "_handle_set_trigger", set_owner)
    monkeypatch.setattr(triggers, "_handle_update_trigger", update_owner)
    monkeypatch.setattr(triggers, "_handle_cancel_trigger", cancel_owner)
    monkeypatch.setattr(triggers, "_handle_list_triggers", list_owner)
    monkeypatch.setattr(agent_tools, "_handle_set_trigger", legacy_owner)
    monkeypatch.setattr(agent_tools, "_handle_update_trigger", legacy_owner)
    monkeypatch.setattr(agent_tools, "_handle_cancel_trigger", legacy_owner)
    monkeypatch.setattr(agent_tools, "_handle_list_triggers", legacy_owner)

    callbacks: tuple[tuple[str, registry.ToolArguments, str], ...] = (
        ("set_trigger", {"name": "daily"}, "set result"),
        ("update_trigger", {"name": "daily"}, "update result"),
        ("cancel_trigger", {"name": "daily"}, "cancel result"),
        ("list_triggers", {}, "list result"),
    )
    for tool_name, arguments, expected in callbacks:
        handler = registry.resolve(tool_name)
        assert handler is not None
        handler_result = handler(
            arguments=arguments,
            agent_id=agent_id,
            user_id=user_id,
            session_id="trigger-session",
            on_output=None,
        )
        assert not isinstance(handler_result, str)
        assert await handler_result == expected

    assert calls == [
        ("set", (agent_id, {"name": "daily"}), {"session_id": "trigger-session", "user_id": user_id}),
        ("update", (agent_id, {"name": "daily"}), {}),
        ("cancel", (agent_id, {"name": "daily"}), {}),
        ("list", (agent_id,), {}),
    ]
