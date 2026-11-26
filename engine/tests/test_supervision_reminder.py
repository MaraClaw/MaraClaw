from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from tests.health_caller_fakes import install_model_pair
from tests.llm_retry_fakes import install_provider_health, trip

from app.services import supervision_reminder
from app.services.llm.provider_health import Admission, is_unavailable, model_key
from app.services.llm.retry_policy import LLMHTTPError
from app.services.llm.types import LLMResponse
from app.services.supervision_reminder import _is_reminder_due


def test_reminder_due_returns_false_for_invalid_json_schedule_interval() -> None:
    # Given: a persisted schedule whose interval is not a number
    now = datetime(2026, 7, 17, 9, 0, tzinfo=UTC)
    last_reminded_at = now - timedelta(days=2)

    # When: the reminder evaluator receives the malformed schedule
    is_due = _is_reminder_due('{"freq":"daily","interval":"invalid","time":"09:00"}', last_reminded_at, now)

    # Then: malformed persisted configuration does not crash the scheduler
    assert is_due is False


@pytest.fixture
def warnings(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    monkeypatch.setattr("app.services.agent_context.build_agent_context", AsyncMock(return_value=("", "")))
    logged: list[str] = []
    monkeypatch.setattr(
        supervision_reminder.logger, "warning", lambda message, *args: logged.append(message.format(*args))
    )
    return logged


async def test_reply_is_skipped_without_a_request_while_the_circuit_is_open(monkeypatch, warnings):
    # Given the target agent's only model has an open circuit.
    tracker, _clock = install_provider_health(monkeypatch, threshold=2)
    pair = install_model_pair(monkeypatch, [LLMResponse(content="unused")])
    trip(tracker, pair.primary, failures=2)

    # When a supervision reminder asks it for a reply.
    reply = await supervision_reminder._get_agent_reply(pair.agent, "Please report")

    # Then there is no reply, no client was built, and the skip was logged once.
    assert reply is None
    assert pair.created == []
    assert pair.primary_client.messages_seen == []
    assert len(warnings) == 1
    assert "unavailable" in warnings[0]


async def test_reply_comes_from_the_fallback_while_the_primary_is_open(monkeypatch, warnings):
    # Given an open primary and a healthy fallback.
    tracker, _clock = install_provider_health(monkeypatch, threshold=2)
    pair = install_model_pair(monkeypatch, [LLMResponse(content="unused")], [LLMResponse(content="On it")])
    trip(tracker, pair.primary, failures=2)

    # When the reminder asks the agent for a reply.
    reply = await supervision_reminder._get_agent_reply(pair.agent, "Please report")

    # Then the fallback answered and the primary was never called.
    assert reply == "On it"
    assert pair.created == ["fallback"]
    assert pair.primary_client.messages_seen == []
    assert warnings == []


async def test_retryable_failures_open_the_circuit_for_the_next_reminder(monkeypatch, warnings):
    # Given a provider that answers 503 and a circuit that opens after two failures.
    _tracker, _clock = install_provider_health(monkeypatch, threshold=2)
    down = LLMHTTPError(503, "upstream busy")
    pair = install_model_pair(monkeypatch, [down, down])

    # When three reminders ask for a reply.
    replies = [await supervision_reminder._get_agent_reply(pair.agent, "Please report") for _ in range(3)]

    # Then none got one, and only the first two reached the provider.
    assert replies == [None, None, None]
    assert len(pair.primary_client.messages_seen) == 2
    assert is_unavailable(pair.primary)


async def test_successful_probe_closes_the_circuit(monkeypatch, warnings):
    # Given an open circuit whose cooldown has ended.
    tracker, clock = install_provider_health(monkeypatch, threshold=2)
    pair = install_model_pair(monkeypatch, [LLMResponse(content="probe"), LLMResponse(content="after")])
    key = model_key(pair.primary)
    assert key is not None
    trip(tracker, pair.primary, failures=2)
    clock.advance(30)

    # When the next reminder is admitted as the probe and the provider answers.
    assert await supervision_reminder._get_agent_reply(pair.agent, "Please report") == "probe"

    # Then the circuit is closed and the following reminder goes out like any other.
    assert tracker.admit(key) is Admission.CALL
    assert await supervision_reminder._get_agent_reply(pair.agent, "Please report") == "after"
