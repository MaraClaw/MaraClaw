import json
import logging
import traceback
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import anyio
import httpx
import pytest
from anyio.abc import SocketAttribute, SocketStream

from app.core.domain.channels import ChannelType
from app.records.channel_config import ChannelConfigRecord
from app.services.channels import telegram_outbound
from app.services.channels.outbound_errors import ProviderDeliveryError, ProviderPayloadRejectedError

TOKEN = "123456:private_bot-token"


def config() -> ChannelConfigRecord:
    return ChannelConfigRecord(
        id=uuid4(), agent_id=uuid4(), channel_type=ChannelType.TELEGRAM, app_id="123456", app_secret=TOKEN
    )


@pytest.fixture
def transport_logs(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> pytest.LogCaptureFixture:
    for name in ("httpx", "httpcore", "httpcore.connection", "httpcore.http11"):
        channel_logger = logging.getLogger(name)
        monkeypatch.setattr(channel_logger, "handlers", [caplog.handler])
        monkeypatch.setattr(channel_logger, "propagate", False)
        monkeypatch.setattr(channel_logger, "disabled", False)
        caplog.set_level(logging.DEBUG, logger=name)
    return caplog


async def test_bot_api_sends_exact_plain_text_and_redacts_httpx_logs(
    monkeypatch: pytest.MonkeyPatch, transport_logs: pytest.LogCaptureFixture
) -> None:
    caplog = transport_logs
    captured: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 17, "future": True}})

    caplog.set_level(logging.DEBUG)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        monkeypatch.setattr(telegram_outbound, "outbound_client", lambda: client)
        message_id = await telegram_outbound.send_telegram_text(config(), "4503599627370495", "<b>plain</b> " * 100)
        assert not client.is_closed
    assert message_id == "17"
    assert len(captured) == 1
    request = captured[0]
    assert request.url.host == "api.telegram.org"
    assert request.url.scheme == "https"
    assert request.url.raw_path == f"/bot{TOKEN}/sendMessage".encode()
    assert json.loads(request.content) == {"chat_id": "4503599627370495", "text": "<b>plain</b> " * 100}
    assert TOKEN not in caplog.text
    assert TOKEN not in repr(request.url)
    assert "HTTP Request: POST https://api.telegram.org/bot[redacted]/sendMessage" in caplog.text


@pytest.mark.parametrize(
    ("status", "body"),
    [
        (500, {"ok": True, "result": {"message_id": 9}}),
        (302, {"ok": True, "result": {"message_id": 9}}),
        (400, {"ok": False, "description": TOKEN}),
        (200, {"ok": False, "error_code": 403, "description": TOKEN}),
        (200, {"ok": True}),
        (200, {"ok": True, "result": {}}),
        (200, {"ok": True, "result": {"message_id": True}}),
        (200, {"ok": "true", "result": {"message_id": 9}}),
        (200, {"ok": True, "result": {"message_id": "9"}}),
        (200, {"ok": True, "result": {"message_id": 0}}),
        (200, []),
    ],
)
async def test_send_requires_http_and_native_acknowledgment(
    monkeypatch: pytest.MonkeyPatch, status: int, body: object
) -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(status, json=body, headers={"Location": f"https://other.example/{TOKEN}"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond), follow_redirects=True) as client:
        monkeypatch.setattr(telegram_outbound, "outbound_client", lambda: client)
        with pytest.raises(ProviderDeliveryError) as error:
            await telegram_outbound.send_telegram_text(config(), "42", "hello")
    assert len(requests) == 1
    assert TOKEN not in str(error.value)
    assert TOKEN not in "".join(traceback.format_exception(error.value))


@pytest.mark.parametrize("failure", ["network", "invalid_json"])
async def test_transport_and_decode_errors_are_sanitized(monkeypatch: pytest.MonkeyPatch, failure: str) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if failure == "network":
            raise httpx.ConnectError(f"failed {TOKEN} {request.url.raw_path!r}", request=request)
        return httpx.Response(200, content=f"invalid-json-{TOKEN}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        monkeypatch.setattr(telegram_outbound, "outbound_client", lambda: client)
        with pytest.raises(ProviderDeliveryError) as error:
            await telegram_outbound.send_telegram_text(config(), "42", "hello")
    assert TOKEN not in "".join(traceback.format_exception(error.value))


async def test_telegram_retry_after_and_http_header_are_respected(monkeypatch: pytest.MonkeyPatch) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            json={"ok": False, "error_code": 429, "parameters": {"retry_after": 60}},
            headers={"Retry-After": "120"},
        )

    before = datetime.now(UTC)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        monkeypatch.setattr(telegram_outbound, "outbound_client", lambda: client)
        with pytest.raises(ProviderDeliveryError) as error:
            await telegram_outbound.send_telegram_text(config(), "42", "hello")
    assert error.value.code == "http_429"
    assert error.value.retry_at is not None
    assert error.value.retry_at >= before + timedelta(seconds=120)


@pytest.mark.parametrize(("destination", "text"), [("@username", "hi"), ("-42", "hi"), ("42", ""), ("42", "x" * 4097)])
async def test_invalid_destination_and_unsplit_text_reject_before_transport(
    monkeypatch: pytest.MonkeyPatch, destination: str, text: str
) -> None:
    def unexpected_client() -> httpx.AsyncClient:
        pytest.fail("Invalid payload reached HTTP transport")

    monkeypatch.setattr(telegram_outbound, "outbound_client", unexpected_client)
    with pytest.raises(ProviderPayloadRejectedError):
        await telegram_outbound.send_telegram_text(config(), destination, text)


async def test_maximum_text_chunk_is_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content) == {"chat_id": "42", "text": "x" * 4096}
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        monkeypatch.setattr(telegram_outbound, "outbound_client", lambda: client)
        assert await telegram_outbound.send_telegram_text(config(), "42", "x" * 4096) == "1"


async def test_real_transport_redacts_echoed_token_from_debug_traces(
    monkeypatch: pytest.MonkeyPatch, transport_logs: pytest.LogCaptureFixture
) -> None:
    caplog = transport_logs
    listener = await anyio.create_tcp_listener(local_host="127.0.0.1", local_port=0)
    port = listener.extra(attribute=SocketAttribute.local_port)

    class LoopbackURL(telegram_outbound._TelegramBotURL):
        def __init__(self, url: str) -> None:
            super().__init__(url, scheme="http", host="127.0.0.1", port=port)

    async def respond(stream: SocketStream) -> None:
        async with stream:
            request = await stream.receive(65536)
            assert f"/bot{TOKEN}/sendMessage".encode() in request
            await stream.send(
                f"HTTP/1.1 302 Found\r\nLocation: https://api.telegram.org/bot{TOKEN}/sendMessage\r\n"
                "Content-Length: 0\r\nConnection: close\r\n\r\n".encode()
            )

    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(telegram_outbound, "_TelegramBotURL", LoopbackURL)
    async with listener, anyio.create_task_group() as tasks:
        tasks.start_soon(listener.serve, respond)
        async with httpx.AsyncClient(trust_env=False) as client:
            monkeypatch.setattr(telegram_outbound, "outbound_client", lambda: client)
            with pytest.raises(ProviderDeliveryError):
                await telegram_outbound.send_telegram_text(config(), "42", "hello")
        tasks.cancel_scope.cancel()
    assert "receive_response_headers.complete" in caplog.text
    assert TOKEN not in caplog.text
