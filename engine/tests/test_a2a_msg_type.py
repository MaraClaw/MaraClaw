"""Tests for async A2A msg_type differentiation (notify/consult/task_delegate).

Validates the branching logic in _send_message_to_agent:
- notify:    fire-and-forget, returns immediately
- task_delegate: async with callback, creates focus + trigger
- consult:   synchronous request-response (original behaviour)
"""

from __future__ import annotations

import json
import uuid
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ── Helpers ──────────────────────────────────────────────────────────


def _make_agent(
    agent_id=None, name="TestAgent", tenant_id=None, agent_type="native", expired=False, primary_model_id=None
):
    agent = MagicMock()
    agent.id = agent_id or uuid.uuid4()
    agent.name = name
    agent.tenant_id = tenant_id or uuid.uuid4()
    agent.agent_type = agent_type
    agent.is_expired = expired
    agent.expires_at = None
    agent.creator_id = uuid.uuid4()
    agent.primary_model_id = primary_model_id
    agent.fallback_model_id = None
    agent.role_description = ""
    agent.max_tool_rounds = 50
    agent.openclaw_last_seen = None
    return agent


def _make_participant(part_id=None, ref_id=None):
    p = MagicMock()
    p.id = part_id or uuid.uuid4()
    p.type = "agent"
    p.ref_id = ref_id or uuid.uuid4()
    return p


def _make_tenant(a2a_async_enabled=True):
    t = MagicMock()
    t.a2a_async_enabled = a2a_async_enabled
    return t


def _make_rel(agent_id, target_agent_id):
    return SimpleNamespace(
        id=uuid.uuid4(),
        agent_id=agent_id,
        target_agent_id=target_agent_id,
        created_by_user_id=None,
    )


async def _active_status(*_args, **_kwargs):
    return {"access_status": "active", "access_status_reason": None, "access_allowed": True}


def _patch_a2a_daos(
    *,
    source_agent,
    target_agent,
    session,
    src_participant,
    tgt_participant,
    tenant=None,
    rel=None,
    primary_model=None,
    history=None,
):
    """Return a list of patch context managers for pure-DAO a2a_context path."""
    tenant = tenant or _make_tenant()
    rel = rel if rel is not None else _make_rel(source_agent.id, target_agent.id)
    history = history if history is not None else []

    async def get_agent(agent_id):
        if agent_id == source_agent.id:
            return source_agent
        if agent_id == target_agent.id:
            return target_agent
        return None

    async def list_by_names_for_tenant(tenant_id, names, **_kwargs):
        if target_agent.name in names and target_agent.tenant_id == tenant_id:
            return [target_agent]
        return []

    async def list_for_agent(agent_id):
        if agent_id == source_agent.id and rel is not None:
            return [rel]
        return []

    async def list_for_agent_with_targets(agent_id):
        return []

    async def get_by_type_ref(type_, ref_id):
        if type_ == "agent" and ref_id == source_agent.id:
            return src_participant
        if type_ == "agent" and ref_id == target_agent.id:
            return tgt_participant
        return None

    async def get_agent_peer_session(**_kwargs):
        return session

    async def insert_message(**_kwargs):
        return SimpleNamespace(id=uuid.uuid4())

    async def update_session(*, db_obj, obj_in):
        for key, value in obj_in.items():
            setattr(db_obj, key, value)
        return db_obj

    async def get_tenant(_tenant_id):
        return tenant

    async def get_model(model_id):
        if primary_model and model_id == primary_model.id:
            return primary_model
        return None

    async def list_recent(**_kwargs):
        return history

    return [
        patch("app.services.agent_tool_exec.a2a_context.agent_dao.get", side_effect=get_agent),
        patch(
            "app.services.agent_tool_exec.a2a_context.agent_dao.list_by_names_for_tenant",
            side_effect=list_by_names_for_tenant,
        ),
        patch(
            "app.services.agent_tool_exec.a2a_context.agent_agent_relationship_dao.list_for_agent",
            side_effect=list_for_agent,
        ),
        patch(
            "app.services.agent_tool_exec.a2a_context.agent_agent_relationship_dao.list_for_agent_with_targets",
            side_effect=list_for_agent_with_targets,
        ),
        patch(
            "app.services.agent_tool_exec.a2a_context.evaluate_agent_relationship_status",
            side_effect=_active_status,
        ),
        patch(
            "app.services.agent_tool_exec.a2a_context.participant_dao.get_by_type_ref",
            side_effect=get_by_type_ref,
        ),
        patch(
            "app.services.agent_tool_exec.a2a_context.chat_session_dao.get_agent_peer_session",
            side_effect=get_agent_peer_session,
        ),
        patch(
            "app.services.agent_tool_exec.a2a_context.chat_message_dao.insert_message",
            side_effect=insert_message,
        ),
        patch(
            "app.services.agent_tool_exec.a2a_context.chat_session_dao.update",
            side_effect=update_session,
        ),
        patch("app.services.agent_tool_exec.a2a_context.tenant_dao.get", side_effect=get_tenant),
        patch("app.services.agent_tool_exec.a2a_context.llm_model_dao.get", side_effect=get_model),
        patch(
            "app.services.agent_tool_exec.a2a_context.chat_message_dao.list_recent",
            side_effect=list_recent,
        ),
        patch(
            "app.services.agent_tool_exec.a2a_handlers.participant_dao.get_by_type_ref",
            side_effect=get_by_type_ref,
        ),
        patch(
            "app.services.agent_tool_exec.a2a_handlers.chat_message_dao.insert_message",
            side_effect=insert_message,
        ),
        patch(
            "app.services.agent_tool_exec.a2a_handlers.gateway_message_dao.create",
            new_callable=AsyncMock,
        ),
    ]


# ── Tests ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_notify_returns_immediately():
    """notify msg_type should return immediately without calling LLM."""
    from app.services.agent_tool_exec.a2a_send import _send_message_to_agent

    from_agent_id = uuid.uuid4()
    target_id = uuid.uuid4()
    session_id = uuid.uuid4()
    src_participant = _make_participant(ref_id=from_agent_id)
    tgt_participant = _make_participant(ref_id=target_id)
    source_agent = _make_agent(from_agent_id, name="Alice")
    target_agent = _make_agent(target_id, name="Bob", tenant_id=source_agent.tenant_id)

    session = MagicMock()
    session.id = session_id
    session.last_message_at = None

    patches = _patch_a2a_daos(
        source_agent=source_agent,
        target_agent=target_agent,
        session=session,
        src_participant=src_participant,
        tgt_participant=tgt_participant,
    )
    with ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        mock_wake = stack.enter_context(patch("app.services.agent_tools._wake_agent_async", new_callable=AsyncMock))
        result = await _send_message_to_agent(
            from_agent_id,
            {
                "agent_name": "Bob",
                "message": "Please review the document",
                "msg_type": "notify",
            },
        )

    assert "Notification sent to Bob" in result
    assert "asynchronously" in result
    mock_wake.assert_awaited_once()


@pytest.mark.asyncio
async def test_task_delegate_creates_focus_and_trigger():
    """task_delegate should create a focus item and an on_message trigger."""
    from app.services.agent_tool_exec.a2a_send import _send_message_to_agent

    from_agent_id = uuid.uuid4()
    target_id = uuid.uuid4()
    session_id = uuid.uuid4()
    src_participant = _make_participant(ref_id=from_agent_id)
    tgt_participant = _make_participant(ref_id=target_id)
    source_agent = _make_agent(from_agent_id, name="Alice")
    target_agent = _make_agent(target_id, name="Bob", tenant_id=source_agent.tenant_id)

    session = MagicMock()
    session.id = session_id
    session.last_message_at = None

    patches = _patch_a2a_daos(
        source_agent=source_agent,
        target_agent=target_agent,
        session=session,
        src_participant=src_participant,
        tgt_participant=tgt_participant,
    )
    with ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        mock_focus = stack.enter_context(patch("app.services.agent_tools._append_focus_item", new_callable=AsyncMock))
        mock_trigger = stack.enter_context(
            patch("app.services.agent_tools._create_on_message_trigger", new_callable=AsyncMock)
        )
        mock_wake = stack.enter_context(patch("app.services.agent_tools._wake_agent_async", new_callable=AsyncMock))
        result = await _send_message_to_agent(
            from_agent_id,
            {
                "agent_name": "Bob",
                "message": "Please prepare the Q3 report",
                "msg_type": "task_delegate",
            },
        )

    assert "Task delegated to Bob" in result
    assert "notified when they complete" in result
    mock_focus.assert_awaited_once()
    mock_trigger.assert_awaited_once()
    mock_wake.assert_awaited_once()

    focus_call = mock_focus.call_args
    assert "wait_bob_task" in focus_call[0][1]
    assert "Bob" in focus_call[0][2]

    trigger_call = mock_trigger.call_args
    assert trigger_call[1]["from_agent_name"] == "Bob"
    assert trigger_call[1]["focus_ref"] == focus_call[0][1]


@pytest.mark.asyncio
async def test_consult_calls_llm_synchronously():
    """consult msg_type should call LLM synchronously and return reply."""
    from app.services.agent_tool_exec.a2a_send import _send_message_to_agent

    from_agent_id = uuid.uuid4()
    target_id = uuid.uuid4()
    session_id = uuid.uuid4()
    model_id = uuid.uuid4()
    src_participant = _make_participant(ref_id=from_agent_id)
    tgt_participant = _make_participant(ref_id=target_id)
    source_agent = _make_agent(from_agent_id, name="Alice")
    target_agent = _make_agent(target_id, name="Bob", tenant_id=source_agent.tenant_id, primary_model_id=model_id)

    session = MagicMock()
    session.id = session_id
    session.last_message_at = None

    model = MagicMock()
    model.id = model_id
    model.provider = "openai"
    model.model = "gpt-4"
    model.api_key_encrypted = "sk-test"
    model.base_url = None
    model.temperature = 0.7
    model.request_timeout = 60

    response = MagicMock()
    response.content = ""
    response.tool_calls = [
        {
            "id": "call_finish",
            "type": "function",
            "function": {
                "name": "finish",
                "arguments": json.dumps({"content": "Here is the answer"}),
            },
        }
    ]
    response.usage = None

    mock_llm_client = AsyncMock()
    mock_llm_client.complete = AsyncMock(return_value=response)
    mock_llm_client.stream = AsyncMock(return_value=response)
    mock_llm_client.close = AsyncMock()

    patches = _patch_a2a_daos(
        source_agent=source_agent,
        target_agent=target_agent,
        session=session,
        src_participant=src_participant,
        tgt_participant=tgt_participant,
        primary_model=model,
    )
    with ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        stack.enter_context(
            patch(
                "app.services.agent_context.build_agent_context",
                new_callable=AsyncMock,
                return_value=("static", "dynamic"),
            )
        )
        stack.enter_context(patch("app.services.llm.caller.create_llm_client", return_value=mock_llm_client))
        stack.enter_context(
            patch("app.services.agent_tools.get_agent_tools_for_llm", new_callable=AsyncMock, return_value=[])
        )
        stack.enter_context(patch("app.services.llm.get_provider_base_url", return_value="https://api.openai.com/v1"))
        stack.enter_context(patch("app.services.token_tracker.record_token_usage", new_callable=AsyncMock))
        stack.enter_context(patch("app.services.activity_logger.log_activity", new_callable=AsyncMock))
        result = await _send_message_to_agent(
            from_agent_id,
            {
                "agent_name": "Bob",
                "message": "What is 2+2?",
                "msg_type": "consult",
            },
        )

    assert "Bob replied" in result
    assert "Here is the answer" in result
    mock_llm_client.stream.assert_awaited()


@pytest.mark.asyncio
async def test_default_msg_type_is_notify():
    """When msg_type is not specified, it should default to notify."""
    from app.services.agent_tool_exec.a2a_send import _send_message_to_agent

    from_agent_id = uuid.uuid4()
    target_id = uuid.uuid4()
    session_id = uuid.uuid4()
    src_participant = _make_participant(ref_id=from_agent_id)
    tgt_participant = _make_participant(ref_id=target_id)
    source_agent = _make_agent(from_agent_id, name="Alice")
    target_agent = _make_agent(target_id, name="Bob", tenant_id=source_agent.tenant_id)

    session = MagicMock()
    session.id = session_id
    session.last_message_at = None

    patches = _patch_a2a_daos(
        source_agent=source_agent,
        target_agent=target_agent,
        session=session,
        src_participant=src_participant,
        tgt_participant=tgt_participant,
    )
    with ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        mock_wake = stack.enter_context(patch("app.services.agent_tools._wake_agent_async", new_callable=AsyncMock))
        result = await _send_message_to_agent(
            from_agent_id,
            {
                "agent_name": "Bob",
                "message": "Heads up about the meeting",
            },
        )

    assert "Notification sent" in result
    mock_wake.assert_awaited_once()


@pytest.mark.asyncio
async def test_missing_agent_name_returns_error():
    """Missing agent_name should return an error."""
    from app.services.agent_tool_exec.a2a_send import _send_message_to_agent

    result = await _send_message_to_agent(
        uuid.uuid4(),
        {
            "agent_name": "",
            "message": "Hello",
        },
    )

    assert "❌" in result


@pytest.mark.asyncio
async def test_no_relationship_returns_error():
    """No relationship between agents should return an error."""
    from app.services.agent_tool_exec.a2a_send import _send_message_to_agent

    from_agent_id = uuid.uuid4()
    target_id = uuid.uuid4()
    source_agent = _make_agent(from_agent_id, name="Alice")
    target_agent = _make_agent(target_id, name="Bob", tenant_id=source_agent.tenant_id)
    src_participant = _make_participant(ref_id=from_agent_id)
    tgt_participant = _make_participant(ref_id=target_id)

    session = MagicMock()
    session.id = uuid.uuid4()

    patches = _patch_a2a_daos(
        source_agent=source_agent,
        target_agent=target_agent,
        session=session,
        src_participant=src_participant,
        tgt_participant=tgt_participant,
        rel=None,
    )
    with ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        stack.enter_context(
            patch(
                "app.services.agent_tool_exec.a2a_context.agent_agent_relationship_dao.list_for_agent",
                new_callable=AsyncMock,
                return_value=[],
            )
        )
        result = await _send_message_to_agent(
            from_agent_id,
            {
                "agent_name": "Bob",
                "message": "Hello",
                "msg_type": "notify",
            },
        )

    assert "do not have a relationship" in result


@pytest.mark.asyncio
async def test_append_focus_item_success():
    """_append_focus_item should call ensure_focus_item."""
    from app.services.agent_tool_exec.a2a_triggers import _append_focus_item

    agent_id = uuid.uuid4()
    with patch(
        "app.services.agent_tool_exec.a2a_triggers.ensure_focus_item",
        new_callable=AsyncMock,
    ) as mock_ensure:
        await _append_focus_item(agent_id, "test_item", "Test description")
        mock_ensure.assert_awaited_once_with(agent_id, focus_ref="test_item", description="Test description")


@pytest.mark.asyncio
async def test_create_on_message_trigger():
    """_create_on_message_trigger should create a trigger in DB."""
    from app.services.agent_tool_exec.a2a_triggers import _create_on_message_trigger

    agent_id = uuid.uuid4()
    created = []

    async def get_by_name(_agent_id, _name):
        return None

    async def create(*, obj_in):
        created.append(obj_in)
        return SimpleNamespace(**obj_in)

    with (
        patch("app.services.agent_tool_exec.a2a_triggers.ensure_focus_item", new_callable=AsyncMock) as mock_ensure,
        patch("app.services.agent_tool_exec.a2a_triggers.agent_trigger_dao.get_by_agent_and_name", get_by_name),
        patch("app.services.agent_tool_exec.a2a_triggers.agent_trigger_dao.create", create),
    ):
        mock_ensure.return_value = "test_focus"

        await _create_on_message_trigger(
            agent_id=agent_id,
            trigger_name="test_trigger",
            from_agent_name="Bob",
            reason="Test reason",
            focus_ref="test_focus",
        )

    assert len(created) == 1
    trigger = created[0]
    assert trigger["name"] == "test_trigger"
    assert trigger["type"] == "on_message"
    assert trigger["config"]["from_agent_name"] == "Bob"
    assert trigger["reason"] == "Test reason"
    assert trigger["focus_ref"] == "test_focus"


@pytest.mark.asyncio
async def test_create_on_message_trigger_resets_fire_count():
    """_create_on_message_trigger should reset fire_count to 0 for an existing trigger."""
    from app.services.agent_tool_exec.a2a_triggers import _create_on_message_trigger

    agent_id = uuid.uuid4()
    existing_trigger = SimpleNamespace(
        agent_id=agent_id,
        name="test_trigger",
        type="on_message",
        config={"from_agent_name": "Bob"},
        reason="Old reason",
        focus_ref="old_focus",
        is_enabled=False,
        fire_count=1,
        last_fired_at=None,
        max_fires=1,
    )
    updates = []

    async def get_by_name(_agent_id, _name):
        return existing_trigger

    async def update(*, db_obj, obj_in):
        updates.append(obj_in)
        for key, value in obj_in.items():
            setattr(db_obj, key, value)
        return db_obj

    with (
        patch("app.services.agent_tool_exec.a2a_triggers.ensure_focus_item", new_callable=AsyncMock) as mock_ensure,
        patch("app.services.agent_tool_exec.a2a_triggers.agent_trigger_dao.get_by_agent_and_name", get_by_name),
        patch("app.services.agent_tool_exec.a2a_triggers.agent_trigger_dao.update", update),
    ):
        mock_ensure.return_value = "new_focus"

        await _create_on_message_trigger(
            agent_id=agent_id,
            trigger_name="test_trigger",
            from_agent_name="Bob",
            reason="New reason",
            focus_ref="new_focus",
        )

    assert existing_trigger.is_enabled is True
    assert existing_trigger.fire_count == 0
    assert existing_trigger.reason == "New reason"
    assert existing_trigger.focus_ref == "new_focus"
    assert updates


@pytest.mark.asyncio
async def test_wake_agent_async_calls_trigger_daemon():
    """_wake_agent_async should delegate to trigger_daemon.wake_agent_with_context."""
    from app.services.agent_tool_exec.a2a_triggers import _wake_agent_async

    agent_id = uuid.uuid4()
    context = "[From Alice] Hello Bob"

    with patch("app.services.trigger_daemon.wake_agent_with_context", new_callable=AsyncMock) as mock_wake:
        await _wake_agent_async(agent_id, context)
        mock_wake.assert_awaited_once_with(
            agent_id,
            context,
            from_agent_id=None,
            skip_dedup=False,
            a2a_session_id=None,
        )


@pytest.mark.asyncio
async def test_openclaw_target_still_queues():
    """OpenClaw targets should still use the gateway queue regardless of msg_type."""
    from datetime import UTC, datetime

    from app.services.agent_tool_exec.a2a_send import _send_message_to_agent

    from_agent_id = uuid.uuid4()
    target_id = uuid.uuid4()
    session_id = uuid.uuid4()
    src_participant = _make_participant(ref_id=from_agent_id)
    tgt_participant = _make_participant(ref_id=target_id)
    source_agent = _make_agent(from_agent_id, name="Alice")
    target_agent = _make_agent(target_id, name="OpenClawBot", agent_type="openclaw", tenant_id=source_agent.tenant_id)
    target_agent.openclaw_last_seen = datetime.now(UTC)

    session = MagicMock()
    session.id = session_id
    session.last_message_at = None

    patches = _patch_a2a_daos(
        source_agent=source_agent,
        target_agent=target_agent,
        session=session,
        src_participant=src_participant,
        tgt_participant=tgt_participant,
    )
    with ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        stack.enter_context(patch("app.services.activity_logger.log_activity", new_callable=AsyncMock))
        result = await _send_message_to_agent(
            from_agent_id,
            {
                "agent_name": "OpenClawBot",
                "message": "Hello",
                "msg_type": "notify",
            },
        )

    assert "OpenClaw agent" in result
    assert "queued" in result


@pytest.mark.asyncio
async def test_feature_flag_off_falls_back_to_consult():
    """When tenant a2a_async_enabled=False, notify and task_delegate fall back to consult."""
    from app.services.agent_tool_exec.a2a_send import _send_message_to_agent

    from_agent_id = uuid.uuid4()
    target_id = uuid.uuid4()
    model_id = uuid.uuid4()
    session_id = uuid.uuid4()
    src_participant = _make_participant(ref_id=from_agent_id)
    tgt_participant = _make_participant(ref_id=target_id)
    source_agent = _make_agent(from_agent_id, name="Alice")
    target_agent = _make_agent(target_id, name="Bob", primary_model_id=model_id, tenant_id=source_agent.tenant_id)

    tenant = MagicMock()
    tenant.a2a_async_enabled = False

    session = MagicMock()
    session.id = session_id
    session.last_message_at = None

    model = MagicMock()
    model.id = model_id
    model.provider = "openai"
    model.model = "gpt-4"
    model.api_key_encrypted = "sk-test"
    model.base_url = None
    model.temperature = 0.7
    model.request_timeout = 60

    response = MagicMock()
    response.content = ""
    response.tool_calls = [
        {
            "id": "call_finish",
            "type": "function",
            "function": {
                "name": "finish",
                "arguments": json.dumps({"content": "Got it"}),
            },
        }
    ]
    response.usage = None

    mock_llm_client = AsyncMock()
    mock_llm_client.complete = AsyncMock(return_value=response)
    mock_llm_client.stream = AsyncMock(return_value=response)
    mock_llm_client.close = AsyncMock()

    patches = _patch_a2a_daos(
        source_agent=source_agent,
        target_agent=target_agent,
        session=session,
        src_participant=src_participant,
        tgt_participant=tgt_participant,
        tenant=tenant,
        primary_model=model,
    )
    with ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        stack.enter_context(
            patch("app.services.agent_context.build_agent_context", new_callable=AsyncMock, return_value=("s", "d"))
        )
        stack.enter_context(patch("app.services.llm.caller.create_llm_client", return_value=mock_llm_client))
        stack.enter_context(
            patch("app.services.agent_tools.get_agent_tools_for_llm", new_callable=AsyncMock, return_value=[])
        )
        stack.enter_context(patch("app.services.llm.get_provider_base_url", return_value="https://api.openai.com/v1"))
        stack.enter_context(patch("app.services.token_tracker.record_token_usage", new_callable=AsyncMock))
        stack.enter_context(patch("app.services.activity_logger.log_activity", new_callable=AsyncMock))
        result = await _send_message_to_agent(
            from_agent_id,
            {
                "agent_name": "Bob",
                "message": "Hello",
                "msg_type": "notify",
            },
        )

    assert "Bob replied" in result
    assert "Got it" in result


@pytest.mark.asyncio
async def test_feature_flag_on_uses_notify():
    """When tenant a2a_async_enabled=True, notify works normally."""
    from app.services.agent_tool_exec.a2a_send import _send_message_to_agent

    from_agent_id = uuid.uuid4()
    target_id = uuid.uuid4()
    session_id = uuid.uuid4()
    src_participant = _make_participant(ref_id=from_agent_id)
    tgt_participant = _make_participant(ref_id=target_id)
    source_agent = _make_agent(from_agent_id, name="Alice")
    target_agent = _make_agent(target_id, name="Bob", tenant_id=source_agent.tenant_id)

    tenant = MagicMock()
    tenant.a2a_async_enabled = True

    session = MagicMock()
    session.id = session_id
    session.last_message_at = None

    patches = _patch_a2a_daos(
        source_agent=source_agent,
        target_agent=target_agent,
        session=session,
        src_participant=src_participant,
        tgt_participant=tgt_participant,
        tenant=tenant,
    )
    with ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        mock_wake = stack.enter_context(patch("app.services.agent_tools._wake_agent_async", new_callable=AsyncMock))
        result = await _send_message_to_agent(
            from_agent_id,
            {
                "agent_name": "Bob",
                "message": "Hello",
                "msg_type": "notify",
            },
        )

    assert "Notification sent" in result
    mock_wake.assert_awaited_once()


@pytest.mark.asyncio
async def test_handle_set_trigger_resets_fire_count():
    """_handle_set_trigger should reset fire_count to 0 if it has reached max_fires when re-enabling."""
    from app.services.agent_tool_exec import _agent_tool_exec_triggers as triggers
    from app.services.agent_tool_exec._agent_tool_exec_triggers import _handle_set_trigger
    from app.services.agent_tool_exec.registry import ToolArguments

    agent_id = uuid.uuid4()
    existing_trigger = SimpleNamespace(
        agent_id=agent_id,
        name="test_trigger",
        type="once",
        config={"at": "2026-03-10T09:00:00+08:00"},
        reason="Old reason",
        focus_ref="old_focus",
        is_enabled=False,
        fire_count=1,
        last_fired_at=None,
        max_fires=1,
    )

    async def get_agent(_id):
        return SimpleNamespace(max_triggers=10)

    async def count_enabled(_agent_id):
        return 0

    async def get_by_name(_agent_id, _name):
        return existing_trigger

    async def update(*, db_obj, obj_in):
        for key, value in obj_in.items():
            setattr(db_obj, key, value)
        return db_obj

    with (
        patch(
            "app.services.agent_tool_exec._agent_tool_exec_triggers.ensure_focus_item", new_callable=AsyncMock
        ) as mock_ensure,
        patch.object(triggers.agent_dao, "get", get_agent),
        patch.object(triggers.agent_trigger_dao, "count_enabled_for_agent", count_enabled),
        patch.object(triggers.agent_trigger_dao, "get_by_agent_and_name", get_by_name),
        patch.object(triggers.agent_trigger_dao, "update", update),
        patch("app.services.audit_logger.write_audit_log", new_callable=AsyncMock),
    ):
        mock_ensure.return_value = "new_focus"

        arguments: ToolArguments = {
            "name": "test_trigger",
            "type": "once",
            "config": {"at": "2026-03-10T09:00:00+08:00"},
            "reason": "New reason",
            "focus_ref": "new_focus",
        }

        result = await _handle_set_trigger(agent_id, arguments)

    assert "re-enabled" in result
    assert existing_trigger.is_enabled is True
    assert existing_trigger.fire_count == 0
    assert existing_trigger.reason == "New reason"


@pytest.mark.asyncio
async def test_execute_tool_failure_writes_system_message(monkeypatch):
    """execute_tool should write a system error message to the session if a messaging tool fails."""
    from app.services.agent_tool_exec.dispatcher import execute_tool

    agent_id = uuid.uuid4()
    user_id = uuid.uuid4()
    session_id = str(uuid.uuid4())
    inserted = []

    async def tenant_lookup(_agent_id: uuid.UUID) -> str:
        return "tenant-msg"

    async def insert_message(**kwargs):
        inserted.append(kwargs)
        return SimpleNamespace(**kwargs)

    async def list_rels(_agent_id):
        return []

    monkeypatch.setattr(
        "app.services.agent_tool_exec.channel_messaging.agent_relationship_dao.list_for_agent_with_members_and_providers",
        list_rels,
    )
    chat_dao_mod = __import__("importlib").import_module("app.dao.chat_dao")

    monkeypatch.setattr(chat_dao_mod.chat_message_dao, "insert_message", insert_message)
    monkeypatch.setattr(
        "app.services.agent_tools._get_agent_tenant_id",
        tenant_lookup,
    )
    monkeypatch.setattr(
        "app.services.activity_logger.log_activity",
        AsyncMock(),
    )

    result = await execute_tool(
        "send_channel_message",
        {
            "member_name": "hi",
            "message": "Hello from Ray",
        },
        agent_id=agent_id,
        user_id=user_id,
        session_id=session_id,
    )

    assert result.startswith("❌")
    assert len(inserted) == 1
    error_msg = inserted[0]
    assert error_msg["conversation_id"] == session_id
    assert error_msg["role"] == "assistant"
    assert "System notice" in error_msg["content"]
    assert "send_channel_message" in error_msg["content"]
