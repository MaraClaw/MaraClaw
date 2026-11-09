import ssl
from typing import Literal

import pytest

from app.core.outbound_policy import OutboundPolicyError
from app.services.agent_tool_exec import images_custom, images_providers
from provider_download_fixtures import dns_answers, download_wire, generation_api, response_bytes
from test_outbound_transport import RecordingBackend

__all__ = ["download_wire"]

Provider = Literal["siliconflow", "openai", "grok", "custom"]
PROVIDERS: tuple[Provider, ...] = ("siliconflow", "openai", "grok", "custom")


async def generate(provider: Provider) -> bytes:
    if provider == "custom":
        return await images_custom._generate_image_custom_api(
            "generation-key",
            "model",
            "https://images.example",
            "/generate",
            "",
            "data.0.url",
            '{"X-API-Key":"custom-secret","Cookie":"custom-cookie=private"}',
            30,
            "draw",
            "1024x1024",
        )
    generators = {
        "siliconflow": images_providers._generate_image_siliconflow,
        "openai": images_providers._generate_image_openai,
        "grok": images_providers._generate_image_grok,
    }
    return await generators[provider]("generation-key", "model", "https://images.example", "draw", "1024x1024")


@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.parametrize("scheme", ["http", "https"])
async def test_generation_result_download_has_no_api_credentials_or_cookie_state(
    download_wire: RecordingBackend, monkeypatch: pytest.MonkeyPatch, provider: Provider, scheme: str
) -> None:
    # The download uses the generation origin, so a reused API cookie jar would leak.
    requests = generation_api(monkeypatch, f"{scheme}://images.example/result.png")

    assert await generate(provider) == b"image-bytes"

    assert requests[0].headers["authorization"] == "Bearer generation-key"
    assert download_wire.connections == ["8.8.8.8"]
    stream = download_wire.streams[0]
    assert b"Host: images.example\r\n" in stream.written
    assert b"authorization:" not in stream.written.lower()
    assert b"cookie:" not in stream.written.lower()
    assert b"x-api-key:" not in stream.written.lower()
    assert stream.tls == (("images.example", ssl.CERT_REQUIRED, True) if scheme == "https" else None)
    assert stream.closed


@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.parametrize(
    "target", ["http://127.0.0.1/secret", "http://169.254.169.254/latest/meta-data", "http://[::1]/"]
)
async def test_generation_rejects_private_result_before_dial(
    download_wire: RecordingBackend, monkeypatch: pytest.MonkeyPatch, provider: Provider, target: str
) -> None:
    generation_api(monkeypatch, target)

    with pytest.raises(OutboundPolicyError):
        await generate(provider)

    assert download_wire.connections == []


@pytest.mark.parametrize("provider", PROVIDERS)
async def test_generation_rejects_redirect_to_private_result(
    download_wire: RecordingBackend, monkeypatch: pytest.MonkeyPatch, provider: Provider
) -> None:
    generation_api(monkeypatch, "https://images.example/result.png")
    download_wire.responses = [response_bytes(b"", status=302, headers="Location: http://127.0.0.1/secret\r\n")]

    with pytest.raises(OutboundPolicyError):
        await generate(provider)

    assert download_wire.connections == ["8.8.8.8"]
    assert download_wire.streams[0].closed


@pytest.mark.parametrize("provider", PROVIDERS)
async def test_generation_rejects_mixed_public_private_dns_without_dial(
    download_wire: RecordingBackend, monkeypatch: pytest.MonkeyPatch, provider: Provider
) -> None:
    generation_api(monkeypatch, "https://images.example/result.png")
    dns_answers(monkeypatch, lambda _: ("8.8.8.8", "10.0.0.1"))

    with pytest.raises(OutboundPolicyError):
        await generate(provider)

    assert download_wire.connections == []


@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.parametrize("fallback", [False, True])
async def test_generation_rechecks_dns_for_redirect_and_failed_dial(
    download_wire: RecordingBackend, monkeypatch: pytest.MonkeyPatch, provider: Provider, fallback: bool
) -> None:
    generation_api(monkeypatch, "https://images.example/result.png")
    answers = iter([("8.8.8.8", "1.1.1.1"), ("127.0.0.1",)])
    dns_answers(monkeypatch, lambda _: next(answers))
    if fallback:
        download_wire.fail_first = True
    else:
        download_wire.responses = [response_bytes(b"", status=302, headers="Location: /next.png\r\n")]

    with pytest.raises(OutboundPolicyError):
        await generate(provider)

    assert download_wire.connections == ["8.8.8.8"]
    assert all(stream.closed for stream in download_wire.streams)
