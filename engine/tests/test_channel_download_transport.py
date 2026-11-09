import json
import ssl
from typing import override
from unittest.mock import AsyncMock

import httpcore
import httpx
import pytest

from app.core.logging import configure_logging
from app.core.outbound_policy import OutboundPolicyError
from app.services import feishu_service
from app.services.channels import dingtalk_downloads
from app.services.channels.media_limits import MediaTooLargeError
from provider_download_fixtures import dns_answers, download_wire, response_bytes
from test_outbound_transport import RecordingBackend, RecordingStream

__all__ = ["download_wire"]


async def feishu_download(monkeypatch: pytest.MonkeyPatch) -> bytes | None:
    monkeypatch.setattr(feishu_service, "get_feishu_app_token", AsyncMock(return_value="feishu-token"))
    return await feishu_service.feishu_service.download_message_resource(
        "app", "secret", "message", "file", resource_type="image", max_bytes=32
    )


async def test_feishu_resource_bearer_uses_fixed_public_origin(
    download_wire: RecordingBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert await feishu_download(monkeypatch) == b"image-bytes"
    assert download_wire.connections == ["8.8.8.8"]
    stream = download_wire.streams[0]
    assert b"Host: open.feishu.cn\r\n" in stream.written
    assert b"Authorization: Bearer feishu-token\r\n" in stream.written
    assert b"GET /open-apis/im/v1/messages/message/resources/file?type=image HTTP/1.1" in stream.written
    assert stream.tls == ("open.feishu.cn", ssl.CERT_REQUIRED, True)
    assert stream.closed


@pytest.mark.parametrize("target", ["https://attacker.example/secret", "http://127.0.0.1/secret"])
async def test_feishu_resource_redirect_never_receives_bearer(
    download_wire: RecordingBackend, monkeypatch: pytest.MonkeyPatch, target: str
) -> None:
    download_wire.responses = [response_bytes(b"", status=302, headers=f"Location: {target}\r\n")]
    with pytest.raises(httpx.HTTPStatusError):
        await feishu_download(monkeypatch)
    assert download_wire.connections == ["8.8.8.8"]
    assert download_wire.streams[0].closed


@pytest.mark.parametrize("addresses", [("127.0.0.1",), ("8.8.8.8", "10.0.0.1")])
async def test_feishu_resource_refuses_private_dns_without_dial(
    download_wire: RecordingBackend, monkeypatch: pytest.MonkeyPatch, addresses: tuple[str, ...]
) -> None:
    dns_answers(monkeypatch, lambda _: addresses)
    with pytest.raises(OutboundPolicyError):
        await feishu_download(monkeypatch)
    assert download_wire.connections == []


@pytest.mark.parametrize("scheme", ["http", "https"])
async def test_dingtalk_exchange_token_never_reaches_temporary_download(
    download_wire: RecordingBackend, scheme: str
) -> None:
    url = f"{scheme}://api.dingtalk.com/temporary?signature=private"
    download_wire.responses = [
        response_bytes(json.dumps({"downloadUrl": url}).encode(), headers="Set-Cookie: token=private; Path=/\r\n"),
        response_bytes(b"", status=302, headers="Location: https://cdn.example/final?signature=private\r\n"),
        response_bytes(),
    ]
    assert await dingtalk_downloads.get_media_download_url("dingtalk-token", "download-code", "robot") == url
    assert await dingtalk_downloads.download_file(url, max_bytes=32) == b"image-bytes"
    assert download_wire.connections == ["8.8.8.8"] * 3
    exchange = download_wire.streams[0]
    assert b"x-acs-dingtalk-access-token: dingtalk-token\r\n" in exchange.written
    assert exchange.tls == ("api.dingtalk.com", ssl.CERT_REQUIRED, True)
    for stream in download_wire.streams[1:]:
        assert b"dingtalk-token" not in stream.written
        assert b"authorization:" not in stream.written.lower()
        assert b"cookie:" not in stream.written.lower()
        assert stream.closed
    assert download_wire.streams[-1].tls == ("cdn.example", ssl.CERT_REQUIRED, True)


@pytest.mark.parametrize("redirect", [False, True])
async def test_dingtalk_private_temporary_download_is_blocked(download_wire: RecordingBackend, redirect: bool) -> None:
    if redirect:
        download_wire.responses = [response_bytes(b"", status=302, headers="Location: http://127.0.0.1/secret\r\n")]
    with pytest.raises(OutboundPolicyError):
        await dingtalk_downloads.download_file("https://cdn.example/start" if redirect else "http://127.0.0.1/", 32)
    assert download_wire.connections == (["8.8.8.8"] if redirect else [])
    assert all(stream.closed for stream in download_wire.streams)


@pytest.mark.parametrize("fallback", [False, True])
async def test_dingtalk_rechecks_dns_for_temporary_download(
    download_wire: RecordingBackend, monkeypatch: pytest.MonkeyPatch, fallback: bool
) -> None:
    answers = iter([("8.8.8.8", "1.1.1.1"), ("127.0.0.1",)])
    dns_answers(monkeypatch, lambda _: next(answers))
    download_wire.fail_first = fallback
    if not fallback:
        download_wire.responses = [response_bytes(b"", status=302, headers="Location: /next\r\n")]
    with pytest.raises(OutboundPolicyError):
        await dingtalk_downloads.download_file("https://cdn.example/start", 32)
    assert download_wire.connections == ["8.8.8.8"]
    assert all(stream.closed for stream in download_wire.streams)


@pytest.mark.parametrize("provider", ["feishu", "dingtalk"])
@pytest.mark.parametrize("failure", ["oversize", "status_503", "read_timeout"])
async def test_channel_public_download_preserves_caps_and_transient_errors(
    download_wire: RecordingBackend, monkeypatch: pytest.MonkeyPatch, provider: str, failure: str
) -> None:
    match failure:
        case "oversize":
            download_wire.responses = [response_bytes(b"x" * 33)]
            expected = MediaTooLargeError
        case "status_503":
            download_wire.responses = [response_bytes(status=503)]
            expected = httpx.HTTPStatusError
        case "read_timeout":

            class SlowStream(RecordingStream):
                @override
                async def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
                    raise httpcore.ReadTimeout("slow provider")

            monkeypatch.setattr("test_outbound_transport.RecordingStream", SlowStream)
            expected = httpx.ReadTimeout
        case _:
            raise AssertionError(failure)
    match provider:
        case "feishu":
            download = feishu_download(monkeypatch)
        case "dingtalk":
            download = dingtalk_downloads.download_file("https://cdn.example/file", 32)
        case _:
            raise AssertionError(provider)
    with pytest.raises(expected):
        await download
    assert download_wire.connections == ["8.8.8.8"]
    assert download_wire.streams[0].closed


async def test_dingtalk_lookup_keeps_transient_failure_and_refusal_semantics(download_wire: RecordingBackend) -> None:
    download_wire.responses = [response_bytes(status=503), response_bytes(b"{}", status=403)]
    with pytest.raises(httpx.HTTPStatusError):
        await dingtalk_downloads.get_media_download_url("token", "code", "robot")
    assert await dingtalk_downloads.get_media_download_url("token", "code", "robot") is None
    assert all(stream.closed for stream in download_wire.streams)


@pytest.mark.parametrize("provider", ["feishu", "dingtalk"])
async def test_channel_public_download_accepts_exact_cap(
    download_wire: RecordingBackend, monkeypatch: pytest.MonkeyPatch, provider: str
) -> None:
    download_wire.responses = [response_bytes(b"x" * 32)]
    match provider:
        case "feishu":
            data = await feishu_download(monkeypatch)
        case "dingtalk":
            data = await dingtalk_downloads.download_file("https://cdn.example/file", 32)
        case _:
            raise AssertionError(provider)
    assert data == b"x" * 32
    assert download_wire.streams[0].closed


async def test_dingtalk_exchange_refuses_redirect_without_sending_token_elsewhere(
    download_wire: RecordingBackend,
) -> None:
    download_wire.responses = [response_bytes(b"", status=302, headers="Location: https://attacker.example/token\r\n")]
    assert await dingtalk_downloads.get_media_download_url("token", "code", "robot") is None
    assert download_wire.connections == ["8.8.8.8"]
    assert download_wire.streams[0].closed


async def test_dingtalk_signed_download_urls_never_enter_debug_logs(download_wire: RecordingBackend) -> None:
    chunks: list[str] = []
    signed = "signed-download-canary"
    download_wire.responses = [
        response_bytes(b"", status=302, headers=f"Location: https://cdn.example/{signed}?signature={signed}\r\n"),
        response_bytes(status=403),
    ]
    configure_logging(level="DEBUG", enqueue=False, sink=chunks.append)
    try:
        assert await dingtalk_downloads.download_file(f"https://cdn.example/start?signature={signed}", 32) is None
        assert "status=403" in "".join(chunks)
        assert signed not in "".join(chunks)
    finally:
        configure_logging()
