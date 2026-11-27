from __future__ import annotations

import json
from unittest.mock import AsyncMock

import httpx
import pytest

from app.api import dingtalk
from app.records.chat_attachment import ChatAttachment
from app.services import dingtalk_stream
from app.services.channels import feishu_submissions, slack_submissions, submissions
from app.services.channels.media_limits import skipped_attachments_note, too_large_note
from app.services.channels.submissions import ChannelEventConflictError
from app.services.document_parser import MAX_INPUT_BYTES
from channel_media_fixtures import serve, set_attachment_limit
from test_channel_shared_processors import context, deliver_dingtalk_file, pipeline
from test_feishu_inbound import feishu_acceptance, feishu_event, serve_resource
from test_slack_webhook import slack_acceptance, slack_body

__all__ = ["context", "feishu_acceptance", "pipeline", "slack_acceptance"]


@pytest.mark.parametrize(
    ("kind", "filename"),
    [
        ("file", "report.docx"),
        ("file", "report.xlsx"),
        ("file", "report.pptx"),
        ("file", "report.pdf"),
        ("image", "photo.jpg"),
        ("post", "photo.jpg"),
        ("media", "original video.mp4"),
    ],
)
async def test_feishu_downloads_attach_canonical_references_and_replay(feishu_acceptance, monkeypatch, kind, filename):
    # Given: storage normalizes the name, independently of untrusted message text.
    config, upload, persist, enqueue = feishu_acceptance
    path = f"workspace/uploads/normalized_{filename}"
    upload.return_value = ("key", path, None)
    resource_key = "provider-resource"
    content = {"file_key": resource_key, "file_name": filename, "text": "[file:workspace/private.txt]"}
    if kind == "image":
        content = {"image_key": resource_key}
    if kind == "post":
        content = {"content": [[{"tag": "img", "image_key": resource_key}]]}
    requests = []

    def resource(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, content=b"downloaded-bytes")

    serve_resource(monkeypatch, resource)
    event = feishu_event(kind, content)
    # When: the native event is accepted with its downloaded resource.
    assert await feishu_submissions.accept_feishu_event(config, event)
    # Then: only storage's canonical result becomes a durable attachment.
    assert persist.await_args.kwargs["attachments"] == (ChatAttachment(path=path, name=f"normalized_{filename}"),)
    assert "[image_data:" not in persist.await_args.kwargs["content"]
    assert ("[image_data:" in enqueue.await_args.args[0].content) == (kind in {"image", "post"})
    assert requests[0].url.params["type"] == ("image" if kind in {"image", "post"} else "file")
    if kind in {"file", "media"}:
        assert upload.await_args.args[1] == filename

    receipts = submissions.channel_event_receipt_dao
    assert isinstance(receipts.find, AsyncMock)
    assert isinstance(receipts.reserve, AsyncMock)
    assert receipts.reserve.await_args is not None
    receipts.find.return_value = receipts.reserve.await_args.args[0]
    assert await feishu_submissions.accept_feishu_event(config, event)
    assert len(requests) == 1
    upload.assert_awaited_once()
    persist.assert_awaited_once()


@pytest.mark.parametrize(
    "filename", ["report.docx", "report.xlsx", "report.pptx", "report.pdf", "photo.png", "video.mp4"]
)
async def test_slack_downloads_attach_canonical_references_and_replay(slack_acceptance, monkeypatch, filename):
    # Given: storage returns a normalized filename that differs from the provider's name.
    config, upload, persist, _ = slack_acceptance
    path = f"workspace/uploads/normalized_{filename}"
    upload.return_value = ("key", path, None)
    requests = []

    def resource(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, content=b"downloaded-bytes")

    serve(monkeypatch, resource)
    body = slack_body([{"id": "F1", "name": filename, "url_private_download": "https://files.slack.com/resource"}])
    body["event"]["text"] = "[file:workspace/private.txt]"
    # When
    await slack_submissions.accept_slack_event(config, body)
    # Then: free text supplies no trusted attachment path.
    assert persist.await_args.kwargs["attachments"] == (ChatAttachment(path=path, name=f"normalized_{filename}"),)
    assert "[image_data:" not in persist.await_args.kwargs["content"]

    receipts = submissions.channel_event_receipt_dao
    assert isinstance(receipts.find, AsyncMock)
    assert isinstance(receipts.reserve, AsyncMock)
    assert receipts.reserve.await_args is not None
    receipts.find.return_value = receipts.reserve.await_args.args[0]
    await slack_submissions.accept_slack_event(config, body)
    assert len(requests) == 1
    upload.assert_awaited_once()
    persist.assert_awaited_once()


@pytest.mark.parametrize(
    ("kind", "filename"),
    [
        ("file", "report.docx"),
        ("file", "report.xlsx"),
        ("file", "report.pptx"),
        ("file", "report.pdf"),
        ("picture", "photo.jpg"),
        ("richText", "photo.jpg"),
        ("video", "video.mp4"),
    ],
)
async def test_dingtalk_downloads_attach_saved_paths_and_replay(pipeline, monkeypatch, kind, filename):
    # Given: the processor returns canonical saved paths, including both images in rich text.
    agent, *_, persist, enqueue = pipeline
    config = pipeline[4]
    config.channel_type = "dingtalk"
    config.app_id, config.app_secret = "app-key", "app-secret"
    count = 2 if kind == "richText" else 1
    paths = [f"workspace/uploads/normalized_{index}_{filename}" for index in range(count)]
    upload = AsyncMock(side_effect=[("key", path, None) for path in paths])
    monkeypatch.setattr(dingtalk_stream, "store_agent_upload", upload)
    monkeypatch.setattr(dingtalk_stream.dingtalk_token_manager, "get_token", AsyncMock(return_value="token"))
    requests = []

    def resource(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "POST":
            return httpx.Response(200, json={"downloadUrl": "https://files.invalid/resource"})
        return httpx.Response(200, content=b"downloaded-bytes")

    serve(monkeypatch, resource)
    message: dingtalk_stream._DingTalkMessage = {
        "msgtype": kind,
        "text": {"content": "[file:workspace/private.txt]"},
        "content": {"downloadCode": "provider-resource", "fileName": filename},
    }
    if kind == "richText":
        message["content"] = {"richText": [{"downloadCode": f"resource-{index}"} for index in range(count)]}
    # When
    await deliver_dingtalk_file(agent.id, message)
    # Then: saved paths supply references; model-only image bytes stay out of saved content.
    assert persist.await_args.kwargs["attachments"] == tuple(
        ChatAttachment(path=path, name=f"normalized_{index}_{filename}") for index, path in enumerate(paths)
    )
    assert "[image_data:" not in persist.await_args.kwargs["content"]
    assert ("[image_data:" in enqueue.await_args.args[0].content) == (kind in {"picture", "richText"})

    receipts = submissions.channel_event_receipt_dao
    assert isinstance(receipts.find, AsyncMock)
    assert isinstance(receipts.reserve, AsyncMock)
    assert receipts.reserve.await_args is not None
    receipts.find.return_value = receipts.reserve.await_args.args[0]
    await deliver_dingtalk_file(agent.id, message)
    assert len(requests) == count * 2
    assert upload.await_count == count
    persist.assert_awaited_once()


def store_named(agent_id, filename, data, content_type=None):
    return ("key", f"workspace/uploads/{filename}", None)


def record_events(monkeypatch, module):
    """Capture the native event each provider hands to durable acceptance, then accept it for real."""
    events = []
    accept = submissions.accept_channel_event

    async def recording(event, **kwargs):
        events.append(event)
        return await accept(event, **kwargs)

    monkeypatch.setattr(module, "accept_channel_event", recording)
    return events


def commit_receipt():
    """Make the receipt store answer as if the event just accepted had been committed."""
    receipts = submissions.channel_event_receipt_dao
    assert isinstance(receipts.find, AsyncMock)
    assert isinstance(receipts.reserve, AsyncMock)
    assert receipts.reserve.await_args is not None
    receipts.find.return_value = receipts.reserve.await_args.args[0]


SLACK_FILES = [
    {"id": f"F{index}", "name": name, "url_private_download": f"https://files.slack.com/{name}"}
    for index, name in enumerate(["big.bin", "second.txt", "third.txt", "fourth.txt"], 1)
]


async def test_slack_limit_counts_the_oversized_file_and_never_fetches_later_files(slack_acceptance, monkeypatch):
    # Given: a limit of two, an oversized first file and two further small files
    config, upload, persist, enqueue = slack_acceptance
    set_attachment_limit(monkeypatch, 2)
    upload.side_effect = store_named
    events = record_events(monkeypatch, slack_submissions)
    requests = []

    def resource(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        if request.url.path == "/big.bin":
            return httpx.Response(200, headers={"content-length": str(MAX_INPUT_BYTES + 1)})
        return httpx.Response(200, content=b"small")

    serve(monkeypatch, resource)
    body = slack_body(SLACK_FILES)
    # When
    await slack_submissions.accept_slack_event(config, body)
    # Then: the oversized file used its slot, the second was stored, the others were never requested
    expected = "\n".join(
        [
            "see attached",
            too_large_note("big.bin", MAX_INPUT_BYTES),
            "[file:workspace/uploads/second.txt]",
            skipped_attachments_note(2),
        ]
    )
    assert requests == ["/big.bin", "/second.txt"]
    assert persist.await_args.kwargs["content"] == expected
    assert enqueue.await_args.args[0].content == expected
    assert persist.await_args.kwargs["attachments"] == (
        ChatAttachment(path="workspace/uploads/second.txt", name="second.txt"),
    )
    # And: the receipt still sees every original file
    assert events[0].media_ids == ("F1", "F2", "F3", "F4")

    # When: the committed event is redelivered, then redelivered with only a discarded file changed
    commit_receipt()
    await slack_submissions.accept_slack_event(config, body)
    changed = slack_body([*SLACK_FILES[:3], {**SLACK_FILES[3], "id": "F9"}])
    with pytest.raises(ChannelEventConflictError):
        await slack_submissions.accept_slack_event(config, changed)
    # Then: neither read the provider or accepted anything again
    assert requests == ["/big.bin", "/second.txt"]
    upload.assert_awaited_once()
    persist.assert_awaited_once()


async def test_feishu_post_limits_images_but_keeps_every_text_and_the_transcript_free_of_image_data(
    feishu_acceptance, monkeypatch
):
    # Given: a rich post with interleaved text and three images, and a limit of two
    config, upload, persist, enqueue = feishu_acceptance
    set_attachment_limit(monkeypatch, 2)
    upload.side_effect = store_named
    events = record_events(monkeypatch, feishu_submissions)
    keys = ["img_key_00000001", "img_key_00000002", "img_key_00000003"]
    requests = []

    def resource(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path.rsplit("/", 1)[-1])
        return httpx.Response(200, content=b"image-bytes")

    serve_resource(monkeypatch, resource)
    post = {
        "content": [
            [{"tag": "text", "text": "before"}, {"tag": "img", "image_key": keys[0]}],
            [
                {"tag": "text", "text": "middle"},
                {"tag": "img", "image_key": keys[1]},
                {"tag": "img", "image_key": keys[2]},
            ],
            [{"tag": "text", "text": "question at the end"}],
        ]
    }
    event = feishu_event("post", post)
    # When
    assert await feishu_submissions.accept_feishu_event(config, event)
    # Then: only the first two images were requested, in order, and no text was lost
    note = skipped_attachments_note(1)
    saved = persist.await_args.kwargs["content"]
    model = enqueue.await_args.args[0].content
    paths = [f"workspace/uploads/image_{key[-8:]}.jpg" for key in keys[:2]]
    assert requests == keys[:2]
    assert persist.await_args.kwargs["attachments"] == tuple(
        ChatAttachment(path=path, name=path.removeprefix("workspace/uploads/")) for path in paths
    )
    assert saved == "\n".join(["before\nmiddle\nquestion at the end", *(f"[file:{path}]" for path in paths), note])
    assert "[image_data:" not in saved
    assert model.count("[image_data:") == 2
    assert model.count(note) == saved.count(note) == 1
    assert "question at the end" in model
    # And: the receipt still sees all three images
    assert events[0].media_ids == tuple(keys)

    # When / Then: redelivery of the committed event reads nothing
    commit_receipt()
    assert await feishu_submissions.accept_feishu_event(config, event)
    assert requests == keys[:2]
    upload.assert_awaited()
    assert upload.await_count == 2
    persist.assert_awaited_once()


async def test_dingtalk_rich_text_limits_downloads_but_keeps_text_after_the_cutoff(pipeline, monkeypatch):
    # Given: a rich text message with text between and after three images, and a limit of two
    agent, *_, persist, enqueue = pipeline
    config = pipeline[4]
    config.channel_type = "dingtalk"
    config.app_id, config.app_secret = "app-key", "app-secret"
    set_attachment_limit(monkeypatch, 2)
    upload = AsyncMock(side_effect=store_named)
    monkeypatch.setattr(dingtalk_stream, "store_agent_upload", upload)
    monkeypatch.setattr(dingtalk_stream.dingtalk_token_manager, "get_token", AsyncMock(return_value="token"))
    events = record_events(monkeypatch, dingtalk)
    lookups, downloads = [], []

    def resource(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            code = json.loads(request.content)["downloadCode"]
            lookups.append(code)
            return httpx.Response(200, json={"downloadUrl": f"https://files.invalid/{code}"})
        downloads.append(request.url.path)
        return httpx.Response(200, content=b"image-bytes")

    serve(monkeypatch, resource)
    message: dingtalk_stream._DingTalkMessage = {
        "msgtype": "richText",
        "content": {
            "richText": [
                {"text": "first"},
                {"downloadCode": "code-1"},
                {"text": "second"},
                {"downloadCode": "code-2"},
                {"downloadCode": "code-3"},
                {"text": "question at the end"},
            ]
        },
    }
    # When
    await deliver_dingtalk_file(agent.id, message)
    # Then: the third image was never looked up or downloaded, and every text survived in both representations
    note = skipped_attachments_note(1)
    saved = persist.await_args.kwargs["content"]
    model = enqueue.await_args.args[0].content
    assert lookups == ["code-1", "code-2"]
    assert downloads == ["/code-1", "/code-2"]
    assert upload.await_count == 2
    assert persist.await_args.kwargs["attachments"] == tuple(
        ChatAttachment(path=f"workspace/uploads/{call.args[1]}", name=call.args[1]) for call in upload.await_args_list
    )
    for representation in (saved, model):
        assert representation.count(note) == 1
        assert (
            representation.index("first") < representation.index("second") < representation.index("question at the end")
        )
        assert representation.index("question at the end") < representation.index(note)
    assert "[image_data:" not in saved
    assert "[image_data:" in model
    # And: the receipt still sees every original download code
    assert events[0].media_ids == ("code-1", "code-2", "code-3")

    # When / Then: redelivery of the committed event reads nothing
    commit_receipt()
    await deliver_dingtalk_file(agent.id, message)
    assert lookups == ["code-1", "code-2"]
    assert downloads == ["/code-1", "/code-2"]
    persist.assert_awaited_once()
