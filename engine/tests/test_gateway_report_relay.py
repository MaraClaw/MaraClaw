import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import anyio
import pytest

from app.services import gateway_report_relay as relay, turn_timing
from app.services.channels.whatsapp_window import WhatsAppReplyWindowError
from gateway_report_relay_fakes import RelayStore


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> RelayStore:
    value = RelayStore()
    value.install(monkeypatch)
    return value


async def test_competing_claims_are_bounded_and_recover_expired_leases(store: RelayStore) -> None:
    # Given
    for _ in range(25):
        store.intent()
    first = await store.claim_batch(limit=20, lease_seconds=60, now=store.now)
    # When
    second = await store.claim_batch(limit=20, lease_seconds=60, now=store.now)
    store.now += timedelta(seconds=60)
    restarted = await store.claim_batch(limit=20, lease_seconds=60, now=store.now)
    # Then
    assert len(first) == len(restarted) == 20
    assert len(second) == 5
    assert {r.id for r in first}.isdisjoint(r.id for r in second)
    assert all(r.claim_token != old.claim_token for r, old in zip(restarted, first, strict=True))
    assert not await store.acknowledge(first[0], now=store.now)
    assert not await store.retry(first[0], available_at=store.now, now=store.now)


@pytest.mark.parametrize("conversation", ["existing-thread", None, ""])
async def test_agent_delivery_is_atomic_and_does_not_repeat_after_restart(
    store: RelayStore, conversation: str | None
) -> None:
    # Given
    row = store.intent()
    store.message.conversation_id = conversation
    # When
    await relay.relay_batch(store.clock)
    await relay.relay_batch(store.clock)
    # Then
    assert store.rows[row.id].delivered_at == store.now
    assert store.wakes == ["authoritative"]
    assert len(store.inbox) == 1
    reply = store.inbox[0]
    assert reply.content == "authoritative"
    assert reply.agent_id == store.destination.id
    assert reply.sender_agent_id == store.source.id
    assert reply.conversation_id == (conversation or f"gw_agent_{store.destination.id}_{store.source.id}")


@pytest.mark.parametrize("channel", ["", "web", "agent", "unknown", "atlassian", "agentbay"])
async def test_im_channel_changes_remain_unacknowledged(store: RelayStore, channel: str) -> None:
    # Given
    row = store.intent("im")
    store.session.source_channel = channel
    # When
    await relay.relay_batch(store.clock)
    # Then
    assert store.sends == []
    assert store.rows[row.id].delivered_at is None
    assert store.rows[row.id].available_at == store.now + timedelta(seconds=1)


@pytest.mark.parametrize("external_id", [None, "", "   "])
async def test_im_missing_external_conversation_remains_unacknowledged(
    store: RelayStore, external_id: str | None
) -> None:
    # Given
    row = store.intent("im")
    store.session.external_conv_id = external_id
    # When
    await relay.relay_batch(store.clock)
    # Then
    assert store.sends == []
    assert store.rows[row.id].delivered_at is None
    assert store.rows[row.id].terminal_at is None


@pytest.mark.parametrize("channel", ["slack", " teams ", "google-chat", "whatsapp", "discord"])
async def test_reply_capable_im_channels_are_delivered(store: RelayStore, channel: str) -> None:
    # Given
    row = store.intent("im")
    store.session.source_channel = channel
    # When
    await relay.relay_batch(store.clock)
    # Then
    assert store.sends == ["authoritative"]
    assert store.rows[row.id].delivered_at == store.now


async def test_canonical_transcript_delivers_to_independent_provider_session(store: RelayStore) -> None:
    # Given a persisted IM intent pointing away from the canonical transcript.
    row = store.intent("im")
    store.message.conversation_id = str(uuid4())
    # When the relay executes the accepted destination.
    await relay.relay_batch(store.clock)
    # Then strict provider output is acknowledged without rewriting the transcript.
    assert store.sends == ["authoritative"]
    assert store.rows[row.id].delivered_at == store.now


@pytest.mark.parametrize("failure", ["ack", "expired_preparation"])
async def test_agent_lost_claim_rolls_back_inbox_and_wake(store: RelayStore, failure: str) -> None:
    # Given
    row = store.intent()
    store.fail_ack = failure == "ack"
    store.preparation_elapsed = 61 if failure == "expired_preparation" else 0
    # When
    await relay.relay_batch(store.clock)
    # Then
    assert store.inbox == store.wakes == []
    assert store.rows[row.id].delivered_at is None


@pytest.mark.parametrize("kind", ["agent", "im"])
@pytest.mark.parametrize("unready", ["missing", "pending", "null", "blank"])
async def test_unavailable_report_remains_retryable(store: RelayStore, kind: str, unready: str) -> None:
    # Given
    row = store.intent(kind)
    if unready == "missing":
        store.message.id = uuid4()
    elif unready == "pending":
        store.message.status = "pending"
    else:
        store.message.result = None if unready == "null" else "   "
    # When
    await relay.relay_batch(store.clock)
    # Then
    assert store.inbox == store.sends == []
    assert store.rows[row.id].delivered_at is None
    assert store.rows[row.id].terminal_at is None
    assert store.rows[row.id].available_at == store.now + timedelta(seconds=1)


@pytest.mark.parametrize("error", [ValueError("config"), RuntimeError("nonack"), OSError("provider"), TimeoutError()])
async def test_provider_failure_retries_only_when_due(store: RelayStore, error: Exception) -> None:
    # Given
    row = store.intent("im")
    store.send_error = error
    await relay.relay_batch(store.clock)
    assert store.rows[row.id].terminal_at is None
    store.send_error = None
    # When
    early = await relay.relay_batch(store.clock)
    store.now += timedelta(seconds=1)
    due = await relay.relay_batch(store.clock)
    # Then
    assert (early, due) == (0, 1)
    assert store.sends == ["authoritative"]
    assert store.rows[row.id].attempts == 2
    assert store.rows[row.id].delivered_at == store.now


async def test_im_success_before_ack_can_repeat_after_lease_expiry(store: RelayStore) -> None:
    # Given
    row = store.intent("im")
    store.crash_after_send = True
    await relay.relay_batch(store.clock)
    assert store.rows[row.id].delivered_at is None
    store.crash_after_send = False
    # When
    await relay.relay_batch(store.clock)
    # Then
    assert store.sends == ["authoritative", "authoritative"]
    assert store.rows[row.id].delivered_at == store.now


async def test_cancellation_leaves_unacknowledged_lease_for_restart(store: RelayStore) -> None:
    # Given
    row = store.intent("im")
    store.block_send = True
    # When
    async with anyio.create_task_group() as tasks:
        tasks.start_soon(relay.relay_batch, store.clock)
        await store.send_entered.wait()
        tasks.cancel_scope.cancel()
    # Then
    assert store.rows[row.id].delivered_at is None
    assert store.rows[row.id].claim_token is not None
    assert store.sends == []
    store.now += timedelta(seconds=60)
    store.block_send = False
    await relay.relay_batch(store.clock)
    assert store.sends == ["authoritative"]


@pytest.mark.parametrize(("attempt", "seconds"), [(0, 1), (1, 1), (2, 2), (9, 256), (10, 300), (10000, 300)])
def test_retry_is_deterministic_and_bounded(attempt: int, seconds: int) -> None:
    # Given / When / Then
    assert relay.retry_delay(attempt) == timedelta(seconds=seconds)


@pytest.fixture
def relay_lines(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, str | int]]:
    captured: list[dict[str, str | int]] = []
    monkeypatch.setattr(
        turn_timing, "logger", SimpleNamespace(info=lambda _t, payload: captured.append(json.loads(payload)))
    )
    return captured


async def test_each_delivery_attempt_logs_one_relay_timing_line(
    store: RelayStore, relay_lines: list[dict[str, str | int]]
) -> None:
    # Given a two-chunk IM report and an agent report, both completed three seconds ago.
    store.message.completed_at = datetime.now(UTC) - timedelta(seconds=3)
    store.message.result = "a" * 4001
    im = store.intent("im")
    agent = store.intent("agent")
    # When the relay delivers the first chunk and the agent report, then the last chunk.
    await relay.relay_batch(store.clock)
    await relay.relay_batch(store.clock)
    # Then every attempt has exactly one line keyed by message and delivery.
    results = {(line["delivery_id"], line["attempt"]): line["result"] for line in relay_lines}
    assert results == {
        (str(im.id), 1): "chunk_sent",
        (str(agent.id), 1): "acknowledged",
        (str(im.id), 2): "acknowledged",
    }
    assert {line["message_id"] for line in relay_lines} == {str(store.message.id)}
    sent = next(line for line in relay_lines if line["result"] == "chunk_sent")
    assert sent["destination_kind"] == "im"
    sent_marks = [mark for key in ("claimed", "sent", "done") if isinstance(mark := sent[key], int)]
    assert len(sent_marks) == 3
    assert min(sent_marks) >= 3000
    assert sent_marks == sorted(sent_marks)
    agent_line = next(line for line in relay_lines if line["destination_kind"] == "agent")
    agent_marks = [mark for key in ("claimed", "done") if isinstance(mark := agent_line[key], int)]
    assert len(agent_marks) == 2
    assert min(agent_marks) >= 3000
    assert agent_marks == sorted(agent_marks)
    assert "sent" not in agent_line


@pytest.mark.parametrize("failure", ["send", "reply_window", "ack"])
async def test_relay_timing_names_retry_terminal_and_lost_results(
    store: RelayStore, relay_lines: list[dict[str, str | int]], failure: str
) -> None:
    store.intent("agent" if failure == "ack" else "im")
    store.fail_send = failure == "send"
    store.fail_ack = failure == "ack"
    if failure == "reply_window":
        store.send_error = WhatsAppReplyWindowError("whatsapp_window_expired")
    await relay.relay_batch(store.clock)
    ((line),) = relay_lines
    assert line["result"] == {"send": "retry", "reply_window": "terminal", "ack": "lost"}[failure]
