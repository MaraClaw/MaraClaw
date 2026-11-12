import asyncio
import json
import uuid
from unittest.mock import AsyncMock, Mock

import pytest
from tests.im_webhook_fixtures import channel_config

from app.records.agent import AgentRecord
from app.records.chat import ChatMessageRecord, ChatSessionRecord
from app.records.user import UserRecord
from app.services import wechat_channel, wechat_message_processor
from app.services.channels import binding, submissions
from app.services.llm.types import OpenAIMessage
from app.services.openclaw_routing import PreparedOpenClawMessage
from worker_progress_fixtures import WorkerHTTP, worker_http

__all__ = ["worker_http"]


async def test_accept_wechat_event_converts_prior_tool_history_before_persisting_new_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = AgentRecord(id=uuid.uuid4(), name="agent", creator_id=uuid.uuid4(), tenant_id=uuid.uuid4())
    sender = UserRecord(id=uuid.uuid4(), tenant_id=agent.tenant_id)
    transcript = ChatSessionRecord(id=uuid.uuid4(), agent_id=agent.id, user_id=agent.creator_id)
    reply = ChatSessionRecord(id=uuid.uuid4(), agent_id=agent.id, user_id=sender.id, source_channel="wechat")
    event = submissions.ChannelEvent(
        config_id=uuid.uuid4(),
        agent_id=agent.id,
        provider="wechat",
        event_key="wechat-bot:chat-1:message-1",
        sender_id="wechat-user",
        conversation_id="wechat_chat-1",
        text="Please check.",
    )
    tool_id = uuid.UUID(int=2)
    history_rows = [
        ChatMessageRecord(
            id=uuid.uuid4(),
            agent_id=agent.id,
            user_id=sender.id,
            conversation_id=str(transcript.id),
            role="user",
            content="What is the status?",
        ),
        ChatMessageRecord(
            id=tool_id,
            agent_id=agent.id,
            user_id=sender.id,
            conversation_id=str(transcript.id),
            role="tool_call",
            content='{"name":"search_docs","args":{"query":"status"},"result":"found status"}',
        ),
    ]
    expected_history: list[OpenAIMessage] = [
        {"role": "user", "content": "What is the status?"},
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": f"call_{tool_id}",
                    "type": "function",
                    "function": {"name": "search_docs", "arguments": json.dumps({"query": "status"})},
                }
            ],
        },
        {"role": "tool", "tool_call_id": f"call_{tool_id}", "content": "found status"},
    ]
    prepared = PreparedOpenClawMessage(agent, event.text, "primary", None, None, "channel-test")
    list_recent = AsyncMock(return_value=history_rows)
    prepare = AsyncMock(return_value=prepared)
    insert_message = AsyncMock(
        return_value=ChatMessageRecord(
            id=uuid.uuid4(),
            agent_id=agent.id,
            user_id=sender.id,
            conversation_id=str(transcript.id),
            role="user",
            content=event.text,
        )
    )
    enqueue = AsyncMock()
    order = Mock()
    order.attach_mock(list_recent, "history")
    order.attach_mock(prepare, "prepare")
    order.attach_mock(insert_message, "persist")
    order.attach_mock(enqueue, "enqueue")
    transaction = Mock(side_effect=AsyncMock)
    monkeypatch.setattr(submissions, "connection_ctx", transaction)
    monkeypatch.setattr(binding, "connection_ctx", transaction)
    monkeypatch.setattr(submissions, "_authorize", AsyncMock(return_value=(agent, sender)))
    monkeypatch.setattr(submissions.channel_event_receipt_dao, "reserve", AsyncMock(return_value=True))
    monkeypatch.setattr(submissions.channel_event_receipt_dao, "lock_conversation", AsyncMock())
    monkeypatch.setattr(submissions, "load_agent_for_new_work", AsyncMock(return_value=agent))
    monkeypatch.setattr(submissions, "charge_conversation_quota", AsyncMock())
    monkeypatch.setattr(submissions, "find_or_create_channel_session", AsyncMock(return_value=reply))
    monkeypatch.setattr(
        submissions,
        "bind_channel_conversation",
        AsyncMock(return_value=binding.ChannelConversation(transcript, reply)),
    )
    monkeypatch.setattr(submissions.chat_session_dao, "lock_for_agent", AsyncMock(return_value=transcript))
    monkeypatch.setattr(submissions.chat_message_dao, "list_recent", list_recent)
    monkeypatch.setattr(submissions.chat_message_dao, "insert_message", insert_message)
    monkeypatch.setattr(submissions.chat_session_dao, "get", AsyncMock(return_value=transcript))
    monkeypatch.setattr(submissions.chat_session_dao, "update", AsyncMock())
    monkeypatch.setattr(submissions, "prepare_shared_openclaw_message", prepare)
    monkeypatch.setattr(submissions, "enqueue_prepared_openclaw_message", enqueue)

    result = await submissions.accept_channel_event(event)

    assert result == submissions.AcceptedChannelEvent(replayed=False)
    list_recent.assert_awaited_once_with(agent_id=agent.id, conversation_id=str(transcript.id), limit=8)
    prepare.assert_awaited_once_with(agent=agent, content="Please check.", history=expected_history)
    insert_message.assert_awaited_once_with(
        agent_id=agent.id,
        user_id=sender.id,
        conversation_id=str(transcript.id),
        role="user",
        content="Please check.",
        thinking=None,
        participant_id=None,
    )
    enqueue.assert_awaited_once_with(
        prepared,
        sender_user_id=sender.id,
        conversation_id=str(transcript.id),
        reply_session_id=reply.id,
        deduplicate=False,
    )
    assert [entry[0] for entry in order.mock_calls] == ["history", "prepare", "persist", "enqueue"]


async def test_process_wechat_message_remembers_context_before_accepting_native_identity_and_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = channel_config("wechat")
    config.extra_config = {"bot_token": "token", "baseurl": "https://wechat.example"}
    expected = submissions.ChannelEvent(
        config_id=config.id,
        agent_id=config.agent_id,
        provider="wechat",
        event_key="bot-app:chat-1:message-1",
        sender_id="wechat-user",
        conversation_id="wechat_chat-1",
        text="Please check.\nThen report.",
    )
    replay = AsyncMock(return_value=False)
    remember = AsyncMock()
    accept = AsyncMock(return_value=submissions.AcceptedChannelEvent(replayed=False))
    order = Mock()
    order.attach_mock(replay, "replay")
    order.attach_mock(remember, "remember")
    order.attach_mock(accept, "accept")
    monkeypatch.setattr(submissions, "is_channel_event_replay", replay)
    monkeypatch.setattr(wechat_channel, "remember_wechat_context", remember)
    monkeypatch.setattr(submissions, "accept_channel_event", accept)

    await wechat_message_processor.process_wechat_message(
        config.agent_id,
        {
            "message_id": "message-1",
            "from_user_id": "wechat-user",
            "session_id": "chat-1",
            "context_token": "context-token",
            "item_list": [
                {"type": 1, "text_item": {"text": "Please check."}},
                {"type": 1, "text_item": {"text": "Then report."}},
            ],
        },
        config,
    )

    replay.assert_awaited_once_with(expected)
    remember.assert_awaited_once_with(
        agent_id=config.agent_id,
        from_user_id="wechat-user",
        context_token="context-token",
        conv_id="wechat_chat-1",
    )
    accept.assert_awaited_once_with(expected)
    assert [entry[0] for entry in order.mock_calls] == ["replay", "remember", "accept"]


def run_reconcile_loop(
    monkeypatch: pytest.MonkeyPatch, worker_http: WorkerHTTP, manager: wechat_channel.WeChatPollManager
) -> tuple[asyncio.Task[object], list[float]]:
    delays: list[float] = []

    async def pace(seconds: float) -> None:
        delays.append(seconds)
        await worker_http.pacing.wait(seconds)

    monkeypatch.setattr(wechat_channel.asyncio, "sleep", pace)
    task = worker_http.start_loop(
        "wechat-reconcile", manager.start_all(), budget_seconds=wechat_channel.PROGRESS_BUDGET_SECONDS
    )
    return task, delays


async def test_start_all_survives_reconcile_failures_with_capped_backoff_and_resets_after_success(
    monkeypatch: pytest.MonkeyPatch, worker_http: WorkerHTTP
) -> None:
    # Given a reconcile pass that fails every time.
    manager = wechat_channel.WeChatPollManager()
    reconcile = AsyncMock(side_effect=OSError("database unavailable"))
    monkeypatch.setattr(manager, "reconcile_clients", reconcile)
    task, delays = run_reconcile_loop(monkeypatch, worker_http, manager)
    await worker_http.pacing.paused()
    # When the loop retries until the backoff is capped.
    for _ in range(6):
        await worker_http.pacing.step()
    base, cap = wechat_channel.RECONCILE_RETRY_BASE_SECONDS, wechat_channel.RECONCILE_RETRY_CAP_SECONDS
    # Then every failed pass waited out a doubling delay that stops at the cap, and the loop is still alive.
    assert delays == [min(cap, base * 2**failures) for failures in range(7)]
    assert delays[-1] == cap < wechat_channel.PROGRESS_BUDGET_SECONDS
    assert not task.done()
    # And a successful pass returns to the regular cadence, after which the next failure starts from the base again.
    reconcile.side_effect = None
    await worker_http.pacing.step()
    reconcile.side_effect = OSError("database unavailable")
    await worker_http.pacing.step()
    assert delays[-2:] == [manager._reconcile_interval_seconds, base]
    assert reconcile.await_count == 9


async def test_start_all_beats_after_every_successful_pass(
    monkeypatch: pytest.MonkeyPatch, worker_http: WorkerHTTP
) -> None:
    # Given a reconcile pass that always succeeds.
    manager = wechat_channel.WeChatPollManager()
    monkeypatch.setattr(manager, "reconcile_clients", AsyncMock())
    task, _ = run_reconcile_loop(monkeypatch, worker_http, manager)
    await worker_http.pacing.paused()
    # When passes keep finishing for longer than the budget in total.
    for _ in range(4):
        await worker_http.pacing.step()
        # Then every finished pass keeps the loop ready.
        await worker_http.ready(expected=True)
    assert not task.done()


async def test_start_all_goes_stale_while_reconcile_keeps_failing_and_recovers_on_success(
    monkeypatch: pytest.MonkeyPatch, worker_http: WorkerHTTP
) -> None:
    # Given a reconcile pass that starts failing after one success.
    manager = wechat_channel.WeChatPollManager()
    reconcile = AsyncMock()
    monkeypatch.setattr(manager, "reconcile_clients", reconcile)
    task, _ = run_reconcile_loop(monkeypatch, worker_http, manager)
    await worker_http.pacing.paused()
    reconcile.side_effect = OSError("database unavailable")
    # When failing passes outlast the budget.
    for _ in range(4):
        await worker_http.pacing.step()
    # Then readiness fails while health holds, and the loop is neither cancelled nor restarted.
    await worker_http.ready(expected=False)
    assert not task.done()
    # And the first successful pass restores readiness.
    reconcile.side_effect = None
    await worker_http.pacing.step()
    await worker_http.ready(expected=True)


async def test_start_all_is_not_kept_ready_by_a_running_poller(
    monkeypatch: pytest.MonkeyPatch, worker_http: WorkerHTTP
) -> None:
    # Given a real poller started by the first pass, and a database that then fails every reconcile pass.
    manager = wechat_channel.WeChatPollManager()
    config = channel_config("wechat")
    config.extra_config = {"bot_token": "token"}
    list_configured = AsyncMock(return_value=[config])
    fetched: asyncio.Queue[None] = asyncio.Queue()
    resume: asyncio.Queue[None] = asyncio.Queue()

    async def fetch_updates(**_kwargs: object) -> dict[str, object]:
        fetched.put_nowait(None)
        await resume.get()
        return {}

    monkeypatch.setattr(wechat_channel.channel_config_dao, "list_configured", list_configured)
    monkeypatch.setattr(manager, "_load_config", AsyncMock(return_value=config))
    monkeypatch.setattr(manager, "_fetch_updates", fetch_updates)
    monkeypatch.setattr(manager, "_set_connected", AsyncMock())
    monkeypatch.setattr(manager, "_update_extra", AsyncMock())
    task, _ = run_reconcile_loop(monkeypatch, worker_http, manager)
    try:
        await worker_http.pacing.paused()
        await asyncio.wait_for(fetched.get(), 2)
        list_configured.side_effect = OSError("database unavailable")
        # When failing passes outlast the budget while the poller keeps completing polls.
        for _ in range(4):
            await worker_http.pacing.step()
            resume.put_nowait(None)
            await asyncio.wait_for(fetched.get(), 2)
        # Then the poller's progress does not hide the failing loop.
        await worker_http.ready(expected=False)
        assert not task.done()
        assert not manager._tasks[config.agent_id].done()
    finally:
        await manager.stop_all()


async def test_start_all_cancels_promptly_while_waiting_out_a_backoff(
    monkeypatch: pytest.MonkeyPatch, worker_http: WorkerHTTP
) -> None:
    # Given a failing pass that is parked in its backoff delay.
    manager = wechat_channel.WeChatPollManager()
    monkeypatch.setattr(manager, "reconcile_clients", AsyncMock(side_effect=OSError("database unavailable")))
    task, _ = run_reconcile_loop(monkeypatch, worker_http, manager)
    await worker_http.pacing.paused()
    # When the loop is cancelled.
    task.cancel()
    done, _ = await asyncio.wait({task}, timeout=2)
    # Then it exits at once, cancelled.
    assert task in done
    assert task.cancelled()


async def test_start_all_cancels_promptly_while_stop_client_awaits_a_poller(
    monkeypatch: pytest.MonkeyPatch, worker_http: WorkerHTTP
) -> None:
    # Given a poller that is slow to finish cancellation, and a removed channel config that makes the pass stop it.
    manager = wechat_channel.WeChatPollManager()
    parked, cleaning_up, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def poller() -> None:
        parked.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cleaning_up.set()
            await release.wait()
            raise

    stuck = asyncio.create_task(poller())
    manager._tasks[uuid.uuid4()] = stuck
    monkeypatch.setattr(wechat_channel.channel_config_dao, "list_configured", AsyncMock(return_value=[]))
    monkeypatch.setattr(manager, "_set_connected", AsyncMock())
    try:
        await asyncio.wait_for(parked.wait(), 2)
        task, _ = run_reconcile_loop(monkeypatch, worker_http, manager)
        await asyncio.wait_for(cleaning_up.wait(), 2)
        # When the loop is cancelled while stop_client is still waiting for that poller.
        task.cancel()
        done, _ = await asyncio.wait({task}, timeout=2)
        # Then the loop exits at once, cancelled, without waiting for the poller.
        assert task in done
        assert task.cancelled()
    finally:
        release.set()
        _ = await asyncio.wait({stuck}, timeout=2)
