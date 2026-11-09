import ssl
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import override
from unittest.mock import AsyncMock

import anyio
import httpx
import pytest

from app.core.outbound_policy import OutboundPolicyError
from app.services.channels import inbound, slack_submissions
from channel_shared_webhook_fixtures import context, pipeline, webhook_pipeline
from provider_download_fixtures import dns_answers, download_wire, ordinary_api, response_bytes
from test_outbound_transport import RecordingBackend, RecordingStream
from test_slack_webhook import legacy_slack, slack_acceptance, slack_body

__all__ = ["context", "download_wire", "legacy_slack", "pipeline", "slack_acceptance", "webhook_pipeline"]


@dataclass(frozen=True)
class SlackDownload:
    run: Callable[[str], Awaitable[None]]
    upload: AsyncMock


@pytest.fixture(params=["native", "fallback"])
def slack_download(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> SlackDownload:
    match request.param:
        case "native":
            config, upload, *_ = request.getfixturevalue("slack_acceptance")

            async def deliver(url: str) -> None:
                assert await slack_submissions.accept_slack_event(
                    config, slack_body([{"id": "F1", "name": "photo.png", "url_private_download": url}])
                )

        case "fallback":
            legacy = request.getfixturevalue("legacy_slack")
            upload = legacy.upload
            monkeypatch.setattr(inbound, "persist_user_message", AsyncMock())

            async def deliver(url: str) -> None:
                await legacy.deliver([{"id": "F1", "name": "photo.png", "url_private_download": url}])

        case _:
            raise AssertionError(request.param)
    ordinary_api(monkeypatch, lambda _: httpx.Response(200, json={"ok": True}))
    return SlackDownload(deliver, upload)


async def test_slack_bearer_stays_on_fixed_origin_across_redirect(
    slack_download: SlackDownload, download_wire: RecordingBackend
) -> None:
    download_wire.responses = [
        response_bytes(b"", status=302, headers="Location: /next.png\r\n"),
        response_bytes(),
    ]

    await slack_download.run("https://files.slack.com/photo.png")

    slack_download.upload.assert_awaited_once()
    assert download_wire.connections == ["8.8.8.8", "8.8.8.8"]
    for stream in download_wire.streams:
        assert b"Host: files.slack.com\r\n" in stream.written
        assert b"Authorization: Bearer xoxb-token\r\n" in stream.written
        assert stream.tls == ("files.slack.com", ssl.CERT_REQUIRED, True)
        assert stream.closed


@pytest.mark.parametrize(
    "target",
    [
        "http://files.slack.com/photo.png",
        "https://files.slack.com:444/photo.png",
        "https://files.slack.com.attacker.example/photo.png",
        "https://attacker.example/photo.png",
        "http://127.0.0.1/secret",
        "https://user:password@files.slack.com/photo.png",
    ],
)
@pytest.mark.parametrize("redirect", [False, True])
async def test_slack_refuses_foreign_initial_and_redirect_targets_before_dial(
    slack_download: SlackDownload, download_wire: RecordingBackend, target: str, redirect: bool
) -> None:
    if redirect:
        download_wire.responses = [response_bytes(b"", status=302, headers=f"Location: {target}\r\n")]

    with pytest.raises(OutboundPolicyError):
        await slack_download.run("https://files.slack.com/photo.png" if redirect else target)

    assert download_wire.connections == (["8.8.8.8"] if redirect else [])
    slack_download.upload.assert_not_awaited()
    assert all(stream.closed for stream in download_wire.streams)


@pytest.mark.parametrize("private_addresses", [("127.0.0.1",), ("8.8.8.8", "10.0.0.1")])
async def test_slack_refuses_private_dns_for_approved_origin(
    slack_download: SlackDownload,
    download_wire: RecordingBackend,
    monkeypatch: pytest.MonkeyPatch,
    private_addresses: tuple[str, ...],
) -> None:
    dns_answers(monkeypatch, lambda _: private_addresses)

    with pytest.raises(OutboundPolicyError):
        await slack_download.run("https://files.slack.com/photo.png")

    assert download_wire.connections == []
    slack_download.upload.assert_not_awaited()


async def test_slack_closes_stream_when_download_is_cancelled(
    slack_download: SlackDownload, download_wire: RecordingBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    download_wire.responses = [b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\n"]
    entered = anyio.Event()

    class BlockingStream(RecordingStream):
        @override
        async def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
            if self.response:
                return await super().read(max_bytes, timeout)
            entered.set()
            await anyio.sleep_forever()
            raise AssertionError("Cancelled body read resumed")

    monkeypatch.setattr("test_outbound_transport.RecordingStream", BlockingStream)

    async def run_download() -> None:
        await slack_download.run("https://files.slack.com/photo.png")

    async with anyio.create_task_group() as tasks:
        tasks.start_soon(run_download)
        await entered.wait()
        tasks.cancel_scope.cancel()

    assert len(download_wire.streams) == 1
    assert download_wire.streams[0].closed
    slack_download.upload.assert_not_awaited()
