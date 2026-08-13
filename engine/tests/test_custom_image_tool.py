import base64
from typing import TypedDict

import pytest
from pydantic import TypeAdapter

from app.core.json_types import JsonValue
from app.services.agent_tools import (
    _custom_image_reference_to_bytes,
    _find_first_image_reference,
    _json_path_get,
    _render_json_template,
)


class _RenderedMessage(TypedDict):
    role: str
    content: str


class _RenderedRequest(TypedDict):
    model: str
    messages: list[_RenderedMessage]


class _RenderedSizedRequest(_RenderedRequest):
    size: str


def test_render_json_template_replaces_placeholders_after_json_parse():
    payload: _RenderedSizedRequest = TypeAdapter[_RenderedSizedRequest](_RenderedSizedRequest).validate_python(
        _render_json_template(
            '{"model":"{model}","messages":[{"role":"user","content":"Draw: {prompt}"}],"size":"{size}"}',
            {
                "model": "google/gemini-2.5-flash-image",
                "prompt": 'red "apple"\nwhite background',
                "size": "1024x1024",
            },
        ),
        strict=True,
    )

    assert payload["model"] == "google/gemini-2.5-flash-image"
    assert payload["messages"][0]["content"] == 'Draw: red "apple"\nwhite background'
    assert payload["size"] == "1024x1024"


def test_render_json_template_accepts_escaped_quote_object_text():
    payload: _RenderedRequest = TypeAdapter[_RenderedRequest](_RenderedRequest).validate_python(
        _render_json_template(
            r"{ \"model\": \"{model}\", \"messages\": [{ \"role\": \"user\", \"content\": \"{prompt}\" }] }",
            {
                "model": "google/gemini-2.5-flash-image",
                "prompt": "red apple",
                "size": "1024x1024",
            },
        ),
        strict=True,
    )

    assert payload["model"] == "google/gemini-2.5-flash-image"
    assert payload["messages"][0]["content"] == "red apple"


def test_render_json_template_accepts_smart_quotes():
    payload: _RenderedRequest = TypeAdapter[_RenderedRequest](_RenderedRequest).validate_python(
        _render_json_template(
            "{ “model”: “{model}”, “messages”: [{ “role”: “user”, “content”: “{prompt}” }] }",
            {
                "model": "google/gemini-2.5-flash-image",
                "prompt": "blue circle",
                "size": "1024x1024",
            },
        ),
        strict=True,
    )

    assert payload["model"] == "google/gemini-2.5-flash-image"
    assert payload["messages"][0]["content"] == "blue circle"


def test_json_path_get_supports_nested_lists_and_dicts():
    data: JsonValue = TypeAdapter[JsonValue](JsonValue).validate_python(
        {"choices": [{"message": {"images": [{"image_url": {"url": "data:image/png;base64,abc"}}]}}]},
        strict=True,
    )

    assert _json_path_get(data, "choices.0.message.images.0.image_url.url") == "data:image/png;base64,abc"
    assert _json_path_get(data, "choices.1.message") is None
    assert _json_path_get(data, "choices.foo.message") is None


@pytest.mark.asyncio
async def test_custom_image_reference_to_bytes_decodes_data_url():
    raw = b"fake-png-bytes"
    data_url = "data:image/png;base64," + base64.b64encode(raw).decode("ascii")

    assert await _custom_image_reference_to_bytes(data_url, client=None) == raw


def test_find_first_image_reference_prefers_known_response_paths():
    image_ref = _find_first_image_reference(
        {
            "choices": [{"message": {"images": [{"image_url": {"url": "https://images.test/known.png"}}]}}],
            "other": {"url": "https://images.test/other.png"},
        }
    )

    assert image_ref == "https://images.test/known.png"


@pytest.mark.asyncio
async def test_custom_image_reference_to_bytes_supports_raw_base64_dict_and_url():
    class FakeResponse:
        content = b"downloaded-image"

        def raise_for_status(self):
            return None

    class FakeClient:
        def __init__(self):
            self.calls = []

        async def get(self, url, **kwargs):
            self.calls.append((url, kwargs["timeout"]))
            return FakeResponse()

    raw = b"raw-image"
    encoded = base64.b64encode(raw).decode("ascii")
    client = FakeClient()

    assert await _custom_image_reference_to_bytes(encoded, client) == raw
    assert await _custom_image_reference_to_bytes({"image_base64": encoded}, client) == raw
    assert await _custom_image_reference_to_bytes("https://images.test/download.png", client) == b"downloaded-image"
    assert client.calls == [("https://images.test/download.png", 60)]
