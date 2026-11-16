import httpx
import pytest
from fastapi import HTTPException
from tests.channel_media_fixtures import CountingStream
from tests.provider_media_fixtures import REAL_CLIENT, media_case

from app.services.channels import teams_media
from app.services.channels.media_limits import READ_CHUNK_BYTES
from app.services.document_parser import IMAGE_UPLOAD_MAX_BYTES, MAX_INPUT_BYTES

HELPERS = ("discord_interaction", "teams", "google_chat")


@pytest.mark.parametrize("name", HELPERS)
@pytest.mark.parametrize("filename", ["photo.png", "file.docx", "clip.mp4"])
async def test_provider_stream_caps_skip_oversize_with_bounded_reads(monkeypatch, tmp_path, name, filename):
    case = media_case(name, monkeypatch, tmp_path)
    case.raw["filename" if name.startswith("discord") else "name" if name == "teams" else "contentName"] = filename
    limit = IMAGE_UPLOAD_MAX_BYTES if filename.endswith(".png") else MAX_INPUT_BYTES
    stream = CountingStream(limit * 2)

    def client(**kwargs):
        return REAL_CLIENT(transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=stream)))

    monkeypatch.setattr(case.module, "public_http_client", client)
    await case.enrich([case.raw])
    assert limit < stream.consumed <= limit + READ_CHUNK_BYTES
    assert not case.accept.call_args.args[0].attachments
    assert "skipped" in case.accept.call_args.kwargs["saved_content"]


@pytest.mark.parametrize("name", HELPERS)
async def test_provider_event_budget_stops_later_media_requests(monkeypatch, tmp_path, name):
    case = media_case(name, monkeypatch, tmp_path)
    streams = []

    def response(request):
        stream = CountingStream(MAX_INPUT_BYTES)
        streams.append(stream)
        return httpx.Response(200, stream=stream)

    monkeypatch.setattr(
        case.module, "public_http_client", lambda **kwargs: REAL_CLIENT(transport=httpx.MockTransport(response))
    )
    await case.enrich([case.raw] * 3)
    assert len(streams) == 1
    assert streams[0].consumed == MAX_INPUT_BYTES
    assert len(case.accept.call_args.args[0].attachments) == 1
    assert case.accept.call_args.kwargs["saved_content"].count("skipped") == 2


@pytest.mark.parametrize("name", HELPERS)
async def test_provider_caps_refs_at_ten_and_normalizes_colliding_filenames(monkeypatch, tmp_path, name):
    case = media_case(name, monkeypatch, tmp_path)
    await case.enrich([case.raw] * 11)
    refs = case.accept.call_args.args[0].attachments
    assert len(refs) == len(case.requests) == 10
    assert len({ref.path for ref in refs}) == 10
    assert all(ref.name == ref.path.removeprefix("workspace/uploads/") for ref in refs)
    assert "maximum 10" in case.accept.call_args.kwargs["saved_content"]


@pytest.mark.parametrize("name", HELPERS)
@pytest.mark.parametrize("status", [302, 500])
async def test_provider_download_never_follows_redirects_or_accepts_failed_http(monkeypatch, tmp_path, name, status):
    case = media_case(name, monkeypatch, tmp_path)
    requests = []

    def response(request):
        requests.append(request)
        return httpx.Response(status, headers={"Location": "https://attacker.example/steal"})

    monkeypatch.setattr(
        case.module,
        "public_http_client",
        lambda **kwargs: REAL_CLIENT(transport=httpx.MockTransport(response), follow_redirects=False),
    )
    with pytest.raises(httpx.HTTPStatusError):
        await case.enrich([case.raw])
    assert len(requests) == 1
    case.accept.assert_not_awaited()


@pytest.mark.parametrize(
    "url",
    [
        "http://cdn.discordapp.com/attachments/1/2/file.docx",
        "https://cdn.discordapp.com.evil.test/attachments/1/2/file.docx",
        "https://cdn.discordapp.com@evil.test/attachments/1/2/file.docx",
        "https://127.0.0.1/attachments/1/2/file.docx",
        "https://cdn.discordapp.com:8443/attachments/1/2/file.docx",
        "https://cdn.discordapp.com/attachments/1/99/file.docx",
    ],
)
async def test_discord_rejects_nonofficial_or_wrong_attachment_resources(monkeypatch, tmp_path, url):
    case = media_case("discord_interaction", monkeypatch, tmp_path)
    case.raw["url"] = url
    with pytest.raises(HTTPException) as error:
        await case.enrich([case.raw])
    assert error.value.status_code == 400
    assert not case.clients
    case.accept.assert_not_awaited()


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.test/file",
        "https://sharepoint.com.evil.test/file",
        "https://user@tenant.sharepoint.com/file",
        "http://tenant.sharepoint.com/file",
        "https://tenant.sharepoint.com:444/file",
    ],
)
async def test_teams_upload_url_never_opens_an_untrusted_origin(monkeypatch, tmp_path, url):
    case = media_case("teams", monkeypatch, tmp_path)
    case.raw["content"] = {"downloadUrl": url, "uniqueId": "file"}
    with pytest.raises(HTTPException) as error:
        await case.enrich([case.raw])
    assert error.value.status_code == 400
    assert not case.clients
    case.token.assert_not_awaited()
    case.accept.assert_not_awaited()


@pytest.mark.parametrize("host", ["smba.trafficmanager.net", "attacker.example"])
async def test_teams_inline_auth_stays_on_authenticated_service_resource(monkeypatch, tmp_path, host):
    case = media_case("teams", monkeypatch, tmp_path)
    raw = {
        "contentType": "image/png",
        "name": "photo.png",
        "contentUrl": f"https://{host}/amer/v3/attachments/one/views/original",
    }
    if host == "attacker.example":
        with pytest.raises(HTTPException) as error:
            await case.enrich([raw])
        assert error.value.status_code == 422
        assert not case.clients
        case.token.assert_not_awaited()
    else:
        await case.enrich([raw])
        assert case.requests[0].headers["authorization"] == "Bearer bot-canary"
        assert case.clients[0]["origin"] == "https://smba.trafficmanager.net"
        assert case.accept.call_args.args[0].attachments[0].path == "workspace/uploads/photo.png"


@pytest.mark.parametrize(
    "resource",
    [
        "https://evil.test/file",
        "spaces/S/messages/../attachments/A",
        "spaces/S/messages/M/attachments/A?alt=evil",
        "spaces/S/messages/M/attachments/A#evil",
    ],
)
async def test_google_uses_resource_name_never_human_viewer_urls(monkeypatch, tmp_path, resource):
    case = media_case("google_chat", monkeypatch, tmp_path)
    case.raw.update({"attachmentDataRef": {"resourceName": resource}, "downloadUri": "https://attacker.example/file"})
    with pytest.raises(HTTPException) as error:
        await case.enrich([case.raw])
    assert error.value.status_code == 422
    assert not case.clients
    case.token.assert_not_awaited()


async def test_google_drive_requires_separate_permission_without_viewer_fallback(monkeypatch, tmp_path):
    case = media_case("google_chat", monkeypatch, tmp_path)
    case.raw.update(
        {
            "driveDataRef": {"driveFileId": "file"},
            "source": "DRIVE_FILE",
            "downloadUri": "https://attacker.example/file",
        }
    )
    with pytest.raises(HTTPException) as error:
        await case.enrich([case.raw])
    assert error.value.status_code == 422
    assert "drive.readonly" in error.value.detail
    assert not case.clients
    case.token.assert_not_awaited()
    case.accept.assert_not_awaited()


def test_teams_ignores_html_and_cards_as_download_sources():
    media = teams_media.parse_teams_media(
        {
            "attachments": [
                {"contentType": "text/html", "content": '<img src="https://attacker.example">'},
                {"contentType": "application/vnd.microsoft.card.adaptive", "content": {"type": "AdaptiveCard"}},
            ]
        }
    )
    assert not media.attachments
