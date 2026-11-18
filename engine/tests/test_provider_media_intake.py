from dataclasses import replace
from pathlib import PurePosixPath
from unittest.mock import AsyncMock

import anyio
import httpx
import pytest
from fastapi import HTTPException
from tests.channel_media_fixtures import set_attachment_limit
from tests.im_webhook_fixtures import AGENT_ID
from tests.provider_media_fixtures import PROVIDERS, REAL_CLIENT, media_case

from app.services.channels import discord_media, google_chat_media, teams_media
from app.services.channels.media_limits import skipped_attachments_note
from app.services.storage_runtime.quota_errors import StorageQuotaError


@pytest.mark.parametrize("name", PROVIDERS)
@pytest.mark.parametrize("caption", ["", "Please inspect this"])
@pytest.mark.parametrize("filename", ["report.docx", "photo.png", "clip.mp4"])
async def test_provider_media_reaches_durable_acceptance_with_canonical_storage_refs(
    monkeypatch, tmp_path, name, caption, filename
):
    case = media_case(name, monkeypatch, tmp_path)
    field = "filename" if name.startswith("discord") else "name" if name == "teams" else "contentName"
    case.raw[field] = filename
    result = await case.invoke(monkeypatch, caption)
    assert result is None or (
        isinstance(result, httpx.Response) and result.status_code == (202 if name == "discord_interaction" else 200)
    )
    native = case.replay.call_args.args[0]
    enriched = case.accept.call_args.args[0]
    assert native.text == caption
    assert enriched.fingerprint() == native.fingerprint()
    assert enriched == replace(native, attachments=enriched.attachments)
    assert len(enriched.attachments) == 1
    ref = enriched.attachments[0]
    assert ref.path == f"workspace/uploads/{filename}"
    assert ref.name == PurePosixPath(ref.path).name
    assert await case.storage.read_bytes(f"{AGENT_ID}/{ref.path}") == b"office"
    assert case.accept.call_args.kwargs == {
        "content": "\n".join(filter(None, (caption, f"[file: {ref.path}]"))),
        "saved_content": "\n".join(filter(None, (caption, f"[file: {ref.path}]"))),
    }
    assert len(case.requests) == (2 if name == "discord_interaction" else 1)
    request = case.requests[-1]
    assert case.clients[-1]["origin"] == f"{request.url.scheme}://{request.url.host}"
    if name == "discord_interaction":
        assert case.requests[0].url.path == "/api/v10/interactions/3/interaction-canary/callback"
        assert case.requests[0].content == b'{"type":5}'
        assert "authorization" not in case.requests[0].headers
        assert case.clients[0]["origin"] == "https://discord.com"
    assert request.headers.get("authorization") == ("Bearer bot-canary" if name == "google_chat" else None)
    if name == "teams":
        case.token.assert_not_awaited()
    if name == "google_chat":
        assert request.url.path == "/v1/media/spaces/S/messages/M/attachments/A"
        assert dict(request.url.params) == {"alt": "media"}


@pytest.mark.parametrize("name", PROVIDERS)
async def test_provider_replay_prevents_all_download_and_storage(monkeypatch, tmp_path, name):
    case = media_case(name, monkeypatch, tmp_path)
    case.replay.return_value = True
    result = await case.invoke(monkeypatch, "")
    assert result is None or (
        isinstance(result, httpx.Response) and result.status_code == (202 if name == "discord_interaction" else 200)
    )
    assert not case.requests
    case.token.assert_not_awaited()
    case.accept.assert_not_awaited()
    assert not await case.storage.list_dir(str(AGENT_ID))


@pytest.mark.parametrize("name", PROVIDERS)
async def test_provider_surfaces_limit_media_before_io_and_keep_the_original_fingerprint(monkeypatch, tmp_path, name):
    # Given: a limit of two and three attachments arriving through the provider's real intake surface
    case = media_case(name, monkeypatch, tmp_path)
    set_attachment_limit(monkeypatch, 2)
    # When
    result = await case.invoke(monkeypatch, "caption", case.distinct(3))
    # Then: the intake succeeded and only the first two attachments were downloaded and stored, in order
    assert result is None or (
        isinstance(result, httpx.Response) and result.status_code == (202 if name == "discord_interaction" else 200)
    )
    native = case.replay.call_args.args[0]
    enriched = case.accept.call_args.args[0]
    downloads = [request for request in case.requests if request.url.host != "discord.com"]
    paths = [f"workspace/uploads/report{index}.docx" for index in range(2)]
    assert len(downloads) == 2
    assert [ref.path for ref in enriched.attachments] == paths
    assert case.accept.call_args.kwargs["saved_content"] == "\n".join(
        ["caption", *(f"[file: {path}]" for path in paths), skipped_attachments_note(1)]
    )
    assert case.accept.call_args.kwargs["content"] == case.accept.call_args.kwargs["saved_content"]
    # And: the receipt still sees all three originals
    assert len(native.media_ids) == 3
    assert enriched.fingerprint() == native.fingerprint()
    assert case.token.await_count == (1 if name == "google_chat" else 0)


@pytest.mark.parametrize("module", [discord_media, teams_media, google_chat_media])
async def test_provider_media_keeps_quota_failure_typed(monkeypatch, tmp_path, module):
    name = "discord_interaction" if module is discord_media else "teams" if module is teams_media else "google_chat"
    case = media_case(name, monkeypatch, tmp_path)
    failure = StorageQuotaError("quota-canary")
    monkeypatch.setattr(module, "store_agent_upload", AsyncMock(side_effect=failure))
    with pytest.raises(StorageQuotaError) as error:
        await case.invoke(monkeypatch, "caption")
    assert error.value is failure
    case.accept.assert_not_awaited()


@pytest.mark.parametrize("module", [discord_media, teams_media, google_chat_media])
async def test_revoked_sender_is_reauthorized_before_media_io(monkeypatch, tmp_path, module):
    name = "discord_interaction" if module is discord_media else "teams" if module is teams_media else "google_chat"
    case = media_case(name, monkeypatch, tmp_path)
    case.replay.side_effect = HTTPException(403, "Sender revoked")
    result = await case.invoke(monkeypatch, "")
    assert isinstance(result, httpx.Response)
    assert result.status_code == 403
    assert not case.requests
    case.token.assert_not_awaited()
    case.accept.assert_not_awaited()


async def test_discord_defers_before_a_blocked_download_and_waits_for_durable_acceptance(monkeypatch, tmp_path):
    case = media_case("discord_interaction", monkeypatch, tmp_path)
    downloading, release = anyio.Event(), anyio.Event()
    calls = []
    results = []

    async def response(request):
        calls.append(request.url.host)
        if request.url.host == "discord.com":
            case.replay.assert_awaited()
            return httpx.Response(204)
        downloading.set()
        await release.wait()
        return httpx.Response(200, content=b"office")

    monkeypatch.setattr(
        discord_media, "public_http_client", lambda **kwargs: REAL_CLIENT(transport=httpx.MockTransport(response))
    )

    async def invoke():
        results.append(await case.invoke(monkeypatch, "caption"))

    async with anyio.create_task_group() as tasks:
        tasks.start_soon(invoke)
        await downloading.wait()
        assert calls == ["discord.com", "cdn.discordapp.com"]
        case.accept.assert_not_awaited()
        assert not results
        release.set()
    assert results[0].status_code == 202
    assert results[0].content == b""
    case.accept.assert_awaited_once()


@pytest.mark.parametrize(
    ("body", "already_acknowledged"),
    [(b'{"code":40060}', True), (b'{"code":50035}', False), (b"", False), (b"invalid-json", False)],
)
async def test_discord_callback_retry_contract_and_errors_are_bounded(
    monkeypatch, tmp_path, body, already_acknowledged
):
    case = media_case("discord_interaction", monkeypatch, tmp_path)
    calls = []

    def response(request):
        calls.append(request.url.host)
        if request.url.host == "discord.com":
            return httpx.Response(400, content=body)
        return httpx.Response(200, content=b"office")

    monkeypatch.setattr(
        discord_media, "public_http_client", lambda **kwargs: REAL_CLIENT(transport=httpx.MockTransport(response))
    )
    result = await case.invoke(monkeypatch, "caption")
    assert isinstance(result, httpx.Response)
    if already_acknowledged:
        assert result.status_code == 202
        case.accept.assert_awaited_once()
    else:
        assert result.status_code == 502
        assert "interaction-canary" not in result.text
        assert calls == ["discord.com"]
        case.accept.assert_not_awaited()
