from typing import override

import httpcore
import httpx
import pytest

from app.core.logging import configure_logging, logger
from app.services import dingtalk_stream
from provider_download_fixtures import download_wire, response_bytes
from test_outbound_transport import RecordingBackend, RecordingStream

__all__ = ["download_wire"]


@pytest.mark.parametrize("failure", ["status_503", "read_timeout"])
async def test_stream_download_preserves_retry_signal_without_signed_urls_in_caller_logs(
    download_wire: RecordingBackend, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    signed = "signed-stream-download-canary"
    url = f"https://cdn.example/{signed}?signature={signed}"
    match failure:
        case "status_503":
            download_wire.responses = [response_bytes(status=503)]
            expected = httpx.HTTPStatusError
        case "read_timeout":

            class SlowStream(RecordingStream):
                @override
                async def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
                    raise httpcore.ReadTimeout(f"slow provider {signed}")

            monkeypatch.setattr("test_outbound_transport.RecordingStream", SlowStream)
            expected = httpx.ReadTimeout
        case _:
            raise AssertionError(failure)
    chunks: list[str] = []
    configure_logging(level="DEBUG", enqueue=False, sink=chunks.append)
    try:
        with pytest.raises(expected) as refused:
            await dingtalk_stream._download_file(url, 32)
        logger.error("DingTalk callback failed: {}", refused.value)
        assert "DingTalk temporary download failed" in "".join(chunks)
        assert signed not in "".join(chunks)
        assert signed.encode() in download_wire.streams[0].written
        assert download_wire.streams[0].closed
    finally:
        configure_logging()
