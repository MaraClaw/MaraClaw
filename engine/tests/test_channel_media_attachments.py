from __future__ import annotations

from unittest.mock import AsyncMock

import httpx
import pytest

from app.records.chat_attachment import ChatAttachment
from app.services import dingtalk_stream
from app.services.channels import feishu_submissions, slack_submissions, submissions
from channel_media_fixtures import serve
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
    assert await slack_submissions.accept_slack_event(config, body)
    # Then: free text supplies no trusted attachment path.
    assert persist.await_args.kwargs["attachments"] == (ChatAttachment(path=path, name=f"normalized_{filename}"),)
    assert "[image_data:" not in persist.await_args.kwargs["content"]

    receipts = submissions.channel_event_receipt_dao
    assert isinstance(receipts.find, AsyncMock)
    assert isinstance(receipts.reserve, AsyncMock)
    assert receipts.reserve.await_args is not None
    receipts.find.return_value = receipts.reserve.await_args.args[0]
    assert await slack_submissions.accept_slack_event(config, body)
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
