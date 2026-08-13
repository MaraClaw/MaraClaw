import sys
import types
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services.agent_tool_exec import platform_messaging

_MISSING = object()


def _objects():
    agent = SimpleNamespace(id=uuid.uuid4(), creator_id=uuid.uuid4(), tenant_id=uuid.uuid4())
    target = SimpleNamespace(id=uuid.uuid4(), username="alex", display_name="Alex")
    relationship = SimpleNamespace(agent_id=agent.id, member_id=uuid.uuid4())
    session = SimpleNamespace(id=uuid.uuid4(), last_message_at=None)
    return agent, target, relationship, session


def _patch(
    monkeypatch,
    events,
    *,
    agent=_MISSING,
    target=_MISSING,
    relationship=_MISSING,
    session=None,
    names=None,
    changed=False,
    status=None,
    fail="",
    insert_fails=False,
):
    default_agent, default_target, default_rel, default_session = _objects()
    if agent is _MISSING:
        agent = default_agent
    if target is _MISSING:
        target = default_target
    if relationship is _MISSING:
        relationship = default_rel
    session = session or default_session

    async def ensure(*_args, **_kwargs):
        events.append("ensure")
        return changed

    async def relationship_status(*_args, **_kwargs):
        return status or {"access_status": "active", "access_status_reason": None}

    async def primary(*_args):
        events.append("primary")
        return session

    async def mark_read(*_args, **_kwargs):
        events.append("read")
        if fail == "read":
            raise RuntimeError("read failed")

    async def session_update(*, db_obj, obj_in):
        events.append("session_update")
        for key, value in obj_in.items():
            setattr(db_obj, key, value)
        return db_obj

    async def insert_message(**_kwargs):
        events.append("insert")
        if insert_fails:
            raise RuntimeError("commit failed")
        return SimpleNamespace(id=uuid.uuid4())

    class _Manager:
        async def send_to_user(self, *_args):
            events.append("websocket")
            if fail == "websocket":
                raise RuntimeError("websocket failed")

    monkeypatch.setattr(platform_messaging.agent_dao, "get", AsyncMock(return_value=agent))
    monkeypatch.setattr(
        platform_messaging.user_dao,
        "find_by_username_or_display_name",
        AsyncMock(return_value=target),
    )
    monkeypatch.setattr(
        platform_messaging.user_dao,
        "list_display_names_for_tenant",
        AsyncMock(return_value=names or ["alex"]),
    )
    monkeypatch.setattr(
        platform_messaging.agent_relationship_dao,
        "get_for_agent_and_user",
        AsyncMock(return_value=relationship),
    )
    monkeypatch.setattr(platform_messaging, "ensure_access_granted_platform_relationships", ensure)
    monkeypatch.setattr(platform_messaging, "evaluate_human_relationship_status", relationship_status)
    monkeypatch.setattr(
        platform_messaging, "logger", SimpleNamespace(debug=lambda *_args: None, exception=lambda *_args: None)
    )
    monkeypatch.setattr(platform_messaging.chat_message_dao, "insert_message", insert_message)
    monkeypatch.setattr(platform_messaging.chat_session_dao, "update", session_update)
    monkeypatch.setitem(
        sys.modules, "app.services.chat_session_service", types.SimpleNamespace(ensure_primary_platform_session=primary)
    )
    monkeypatch.setitem(
        sys.modules,
        "app.api.websocket",
        types.SimpleNamespace(maybe_mark_session_read_for_active_viewer=mark_read, manager=_Manager()),
    )
    return session


async def test_validation_happens_before_opening_a_session(monkeypatch):
    get_agent = AsyncMock(side_effect=AssertionError("must not open DB"))
    monkeypatch.setattr(platform_messaging.agent_dao, "get", get_agent)
    arguments = {"username": " ", "message": "hello"}

    result = await platform_messaging._send_platform_message(uuid.uuid4(), arguments)

    assert result == "❌ Please provide recipient username and message content"
    get_agent.assert_not_awaited()
    assert arguments == {"username": " ", "message": "hello"}


async def test_missing_agent_returns_before_recipient_lookup(monkeypatch):
    events = []
    _patch(monkeypatch, events, agent=None, target=None, relationship=None)

    result = await platform_messaging._send_platform_message(uuid.uuid4(), {"username": "alex", "message": "hello"})

    assert result == "❌ Agent not found"


@pytest.mark.parametrize(("changed", "expect_ensure"), [(False, True), (True, True)])
async def test_relationship_bootstrap_is_invoked(monkeypatch, changed, expect_ensure):
    events = []
    agent, target, relationship, session = _objects()
    _patch(monkeypatch, events, agent=agent, target=target, relationship=relationship, session=session, changed=changed)

    await platform_messaging._send_platform_message(agent.id, {"username": "alex", "message": "hello"})

    assert ("ensure" in events) is expect_ensure


async def test_missing_recipient_lists_available_users(monkeypatch):
    events = []
    agent, _target, _relationship, session = _objects()
    _patch(monkeypatch, events, agent=agent, target=None, session=session, names=["alex"])

    result = await platform_messaging._send_platform_message(agent.id, {"username": "missing", "message": "hello"})

    assert result == "❌ No user named 'missing' found in your organization. Available users: alex"


@pytest.mark.parametrize(
    ("relationship", "status", "expected"),
    [
        (None, None, "❌ Alex is not in your active relationship network"),
        (
            SimpleNamespace(agent_id=uuid.uuid4(), member_id=uuid.uuid4()),
            {"access_status": "restricted", "access_status_reason": "denied"},
            "❌ Relationship to Alex is not active (denied)",
        ),
    ],
)
async def test_relationship_permissions_block_persistence(monkeypatch, relationship, status, expected):
    events = []
    agent, target, _rel, session = _objects()
    _patch(
        monkeypatch,
        events,
        agent=agent,
        target=target,
        relationship=relationship,
        session=session,
        status=status,
    )

    result = await platform_messaging._send_platform_message(agent.id, {"username": "alex", "message": "hello"})

    assert result == expected
    assert "insert" not in events


async def test_persists_before_websocket_and_preserves_success_text(monkeypatch):
    events = []
    agent, target, relationship, session = _objects()
    _patch(monkeypatch, events, agent=agent, target=target, relationship=relationship, session=session)

    result = await platform_messaging._send_platform_message(agent.id, {"username": "alex", "message": "hello"})

    assert result == "✅ Message sent to Alex on web platform. It has been saved to their chat history."
    assert events[-5:] == ["primary", "insert", "session_update", "read", "websocket"]
    assert session.last_message_at is not None


async def test_insert_failure_prevents_websocket_dispatch(monkeypatch):
    events = []
    agent, target, relationship, session = _objects()
    _patch(
        monkeypatch,
        events,
        agent=agent,
        target=target,
        relationship=relationship,
        session=session,
        insert_fails=True,
    )

    result = await platform_messaging._send_platform_message(agent.id, {"username": "alex", "message": "hello"})

    assert result == "❌ Web message send error: commit failed"
    assert "insert" in events
    assert "websocket" not in events


@pytest.mark.parametrize("fail", ["read", "websocket"])
async def test_read_and_websocket_failures_are_swallowed_after_persistence(monkeypatch, fail):
    events = []
    agent, target, relationship, session = _objects()
    _patch(monkeypatch, events, agent=agent, target=target, relationship=relationship, session=session, fail=fail)

    result = await platform_messaging._send_platform_message(agent.id, {"username": "alex", "message": "hello"})

    assert result.startswith("✅ Message sent to Alex")
    assert "insert" in events


async def test_outer_error_is_truncated_to_200_characters(monkeypatch):
    async def fail_get(*_a, **_k):
        raise RuntimeError("x" * 250)

    monkeypatch.setattr(platform_messaging.agent_dao, "get", fail_get)
    monkeypatch.setattr(platform_messaging, "logger", SimpleNamespace(exception=lambda *_args: None))

    result = await platform_messaging._send_platform_message(uuid.uuid4(), {"username": "alex", "message": "hello"})

    assert result == f"❌ Web message send error: {'x' * 200}"
