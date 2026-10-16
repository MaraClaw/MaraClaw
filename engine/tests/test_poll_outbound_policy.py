import socket
import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import httpx
import pytest

from app.core.json_types import JsonObject
from app.core.outbound_policy import OutboundPolicyError
from app.records.trigger import AgentTriggerRecord
from app.services.trigger_runtime import evaluator


def _trigger(config: JsonObject) -> AgentTriggerRecord:
    return AgentTriggerRecord(
        id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        name="outbound-poll",
        reason="test",
        is_enabled=True,
        expires_at=None,
        max_fires=None,
        fire_count=0,
        last_fired_at=None,
        cooldown_seconds=0,
        config=config,
        type="poll",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/",
        "http://[::1]/",
        "http://169.254.169.254/",
        "https://198.18.0.1/",
        "https://benchmark.example/",
        "https://mixed.example/",
        "https://missing.example/",
        "http://2130706433/",
        "http://[::ffff:127.0.0.1]/",
        "ftp://public.example/file",
        "public.example/path",
        "https://user:secret@public.example/",
    ],
)
async def test_poll_blocks_forbidden_destinations_before_client_creation(monkeypatch, url) -> None:
    def resolve(host: str, port: int, *, type: socket.SocketKind):
        assert type == socket.SOCK_STREAM
        if host == "missing.example":
            raise socket.gaierror(socket.EAI_NONAME, "not found")
        addresses = ["93.184.216.34", "10.0.0.1"] if host == "mixed.example" else ["198.18.0.1"]
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port)) for address in addresses]

    def forbidden_client(**_kwargs):
        pytest.fail("Blocked destination reached the HTTP client")

    persist = AsyncMock()
    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    monkeypatch.setattr(evaluator, "public_http_client", forbidden_client)
    monkeypatch.setattr(evaluator.agent_trigger_dao, "get", persist)

    result = await evaluator.poll_check(_trigger({"url": url}))

    assert result is False
    persist.assert_not_awaited()


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ({}, False),
        ({"_last_value": "ready"}, False),
        ({"_last_value": "pending"}, True),
        ({"fire_on": "match", "match_value": "ready"}, True),
        ({"fire_on": "match", "match_value": "pending"}, False),
    ],
)
async def test_public_poll_preserves_fire_and_persistence(monkeypatch, state, expected) -> None:
    def resolve(host: str, port: int, *, type: socket.SocketKind):
        assert host == "public.example"
        return [(socket.AF_INET, type, 6, "", ("93.184.216.34", port))]

    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"result": {"state": "ready"}})

    def client(*, timeout: float, follow_redirects: bool) -> httpx.AsyncClient:
        assert timeout == 10
        assert follow_redirects is False
        return httpx.AsyncClient(transport=httpx.MockTransport(respond), follow_redirects=follow_redirects)

    trigger = _trigger(
        {
            "url": "https://public.example/poll",
            "method": "POST",
            "headers": {"X-Poll": "yes"},
            "json_path": "$.result.state",
            **state,
        }
    )
    persist = AsyncMock()
    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    monkeypatch.setattr(evaluator, "public_http_client", client)
    monkeypatch.setattr(evaluator.agent_trigger_dao, "get", AsyncMock(return_value=trigger))
    monkeypatch.setattr(evaluator.agent_trigger_dao, "update", persist)

    result = await evaluator.poll_check(trigger)

    assert result is expected
    assert len(requests) == 1
    assert requests[0].method == "POST"
    assert requests[0].headers["X-Poll"] == "yes"
    assert str(requests[0].url) == "https://public.example/poll"
    persist.assert_awaited_once_with(db_obj=trigger, obj_in={"config": {**trigger.config, "_last_value": "ready"}})


async def test_poll_connection_policy_error_does_not_persist(monkeypatch) -> None:
    async def validate(url: str) -> str:
        return url

    def blocked_client(**_kwargs):
        raise OutboundPolicyError("Private, local, reserved, or internal network URLs are blocked (127.0.0.1)")

    persist = AsyncMock()
    monkeypatch.setattr(evaluator, "validate_public_url", validate)
    monkeypatch.setattr(evaluator, "public_http_client", blocked_client)
    monkeypatch.setattr(evaluator.agent_trigger_dao, "get", persist)

    result = await evaluator.poll_check(_trigger({"url": "https://public.example/poll"}))

    assert result is False
    persist.assert_not_awaited()


async def test_poll_does_not_follow_redirect_or_persist_redirect_response(monkeypatch) -> None:
    async def validate(url: str) -> str:
        return url

    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(302, headers={"location": "http://127.0.0.1/private"})

    def client(*, timeout: float, follow_redirects: bool) -> httpx.AsyncClient:
        assert follow_redirects is False
        return httpx.AsyncClient(transport=httpx.MockTransport(respond), follow_redirects=follow_redirects)

    persist = AsyncMock()
    monkeypatch.setattr(evaluator, "validate_public_url", validate)
    monkeypatch.setattr(evaluator, "public_http_client", client)
    monkeypatch.setattr(evaluator.agent_trigger_dao, "get", persist)

    result = await evaluator.poll_check(_trigger({"url": "https://public.example/poll"}))

    assert result is False
    assert [str(request.url) for request in requests] == ["https://public.example/poll"]
    persist.assert_not_awaited()
