import base64
import re
import sys
import types
import uuid
from collections import deque
from unittest.mock import AsyncMock

import pytest

from app.services import agent_tools
from app.services.agent_tool_exec import images


class FakeResponse:
    def __init__(self, status_code, payload=None, *, text="", content=b""):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text
        self.content = content

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeAsyncClient:
    def __init__(self, transport, client_kwargs):
        self._transport = transport
        self._client_kwargs = client_kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, _exc_type, _exc, _traceback):
        return False

    async def post(self, url, **kwargs):
        return self._transport.request("POST", url, self._client_kwargs, kwargs)

    async def get(self, url, **kwargs):
        return self._transport.request("GET", url, self._client_kwargs, kwargs)


class FakeTimeoutError(Exception):
    pass


class FakeHTTPX:
    def __init__(self, responses):
        self._responses = deque(responses)
        self.calls = []
        self.__dict__["TimeoutException"] = FakeTimeoutError
        self.__dict__["AsyncClient"] = self._new_async_client

    def _new_async_client(self, **kwargs):
        return FakeAsyncClient(self, kwargs)

    def request(self, method, url, client_kwargs, kwargs):
        self.calls.append((method, url, client_kwargs, kwargs))
        if not self._responses:
            raise AssertionError(f"unexpected {method} request to {url}")
        response = self._responses.popleft()
        if isinstance(response, Exception):
            raise response
        return response


def install_fake_httpx(monkeypatch, responses):
    fake_httpx = FakeHTTPX(responses)
    monkeypatch.setitem(sys.modules, "httpx", fake_httpx)
    return fake_httpx


@pytest.mark.asyncio
async def test_upload_image_uploads_workspace_file_with_imagekit_request(monkeypatch, tmp_path):
    image_bytes = b"image-bytes"
    (tmp_path / "photo.png").write_bytes(image_bytes)
    fake_httpx = install_fake_httpx(
        monkeypatch,
        [FakeResponse(201, {"url": "https://cdn.test/photo.png", "fileId": "file-1", "size": 2048, "name": "cdn.png"})],
    )
    config = AsyncMock(return_value={"private_key": "private-key"})
    monkeypatch.setattr(agent_tools, "_get_tool_config", config)

    result = await agent_tools._upload_image(uuid.uuid4(), tmp_path, {"file_path": "photo.png"})

    auth = base64.b64encode(b"private-key:").decode()
    assert result == (
        "✅ Image uploaded successfully!\n\n**CDN URL**: https://cdn.test/photo.png\n"
        "**File ID**: file-1\n**Size**: 2.0KB\n**Name**: cdn.png"
    )
    config.assert_awaited_once()
    assert fake_httpx.calls == [
        (
            "POST",
            "https://upload.imagekit.io/api/v2/files/upload",
            {"timeout": 60},
            {
                "headers": {"Authorization": f"Basic {auth}"},
                "data": {"fileName": "photo.png", "folder": "/maraclaw", "useUniqueFileName": "true"},
                "files": {"file": ("photo.png", image_bytes)},
            },
        )
    ]


@pytest.mark.asyncio
async def test_upload_image_returns_http_failure_without_retrying(monkeypatch, tmp_path):
    (tmp_path / "photo.png").write_bytes(b"image-bytes")
    fake_httpx = install_fake_httpx(monkeypatch, [FakeResponse(502, text="upstream unavailable")])
    monkeypatch.setattr(agent_tools, "_get_tool_config", AsyncMock(return_value={"private_key": "private-key"}))

    result = await agent_tools._upload_image(uuid.uuid4(), tmp_path, {"file_path": "photo.png"})

    assert result == "❌ Upload failed (HTTP 502): upstream unavailable"
    assert len(fake_httpx.calls) == 1


@pytest.mark.asyncio
async def test_siliconflow_generates_then_downloads_url_with_expected_timeouts(monkeypatch):
    fake_httpx = install_fake_httpx(
        monkeypatch,
        [
            FakeResponse(200, {"data": [{"url": "https://images.test/generated.png"}]}),
            FakeResponse(200, content=b"siliconflow-bytes"),
        ],
    )

    result = await agent_tools._generate_image_siliconflow(
        "key", "flux", "https://api.test/v1/", "draw a fox", "768x1024"
    )

    assert result == b"siliconflow-bytes"
    assert fake_httpx.calls == [
        (
            "POST",
            "https://api.test/v1/images/generations",
            {"timeout": 120},
            {
                "json": {"model": "flux", "prompt": "draw a fox", "image_size": "768x1024", "n": 1},
                "headers": {"Authorization": "Bearer key", "Content-Type": "application/json"},
            },
        ),
        ("GET", "https://images.test/generated.png", {"timeout": 120}, {"timeout": 60}),
    ]


@pytest.mark.asyncio
async def test_openai_and_google_preserve_provider_payloads(monkeypatch):
    image_bytes = b"provider-image"
    encoded = base64.b64encode(image_bytes).decode()
    fake_httpx = install_fake_httpx(
        monkeypatch,
        [
            FakeResponse(200, {"data": [{"b64_json": encoded}]}),
            FakeResponse(200, {"candidates": [{"content": {"parts": [{"inlineData": {"data": encoded}}]}}]}),
        ],
    )

    openai_result = await agent_tools._generate_image_openai(
        "openai-key", "gpt-image", "https://openai.test/v1", "draw a cat", "1024x768"
    )
    google_result = await agent_tools._generate_image_google(
        "google-key", "gemini-image", "https://google.test/v1", "draw a bird", "1366x768"
    )

    assert openai_result == image_bytes
    assert google_result == image_bytes
    assert fake_httpx.calls == [
        (
            "POST",
            "https://openai.test/v1/images/generations",
            {"timeout": 120},
            {
                "json": {
                    "model": "gpt-image",
                    "prompt": "draw a cat",
                    "size": "1024x768",
                    "n": 1,
                    "response_format": "b64_json",
                },
                "headers": {"Authorization": "Bearer openai-key", "Content-Type": "application/json"},
            },
        ),
        (
            "POST",
            "https://google.test/v1/models/gemini-image:generateContent",
            {"timeout": 120},
            {
                "json": {
                    "contents": [{"parts": [{"text": "draw a bird"}]}],
                    "generationConfig": {"responseModalities": ["IMAGE"], "imageConfig": {"aspectRatio": "16:9"}},
                },
                "headers": {"Content-Type": "application/json", "x-goog-api-key": "google-key"},
            },
        ),
    ]


@pytest.mark.asyncio
async def test_grok_preserves_imagine_payload_and_maps_size_to_aspect_ratio(monkeypatch):
    image_bytes = b"grok-image"
    encoded = base64.b64encode(image_bytes).decode()
    fake_httpx = install_fake_httpx(monkeypatch, [FakeResponse(200, {"data": [{"b64_json": encoded}]})])

    result = await agent_tools._generate_image_grok(
        "xai-key", "grok-imagine-image-2.0", "https://api.x.ai/v1/", "draw a fox", "1366x768"
    )

    assert result == image_bytes
    assert fake_httpx.calls == [
        (
            "POST",
            "https://api.x.ai/v1/images/generations",
            {"timeout": 120},
            {
                "json": {
                    "model": "grok-imagine-image-2.0",
                    "prompt": "draw a fox",
                    "n": 1,
                    "response_format": "b64_json",
                    "aspect_ratio": "16:9",
                    "resolution": "1k",
                },
                "headers": {"Authorization": "Bearer xai-key", "Content-Type": "application/json"},
            },
        )
    ]


def test_grok_size_maps_to_imagine_aspect_ratio_and_resolution():
    from app.services.agent_tool_exec import images_providers as providers

    assert providers._size_to_aspect_ratio("1024x1536") == "2:3"
    assert providers._size_to_aspect_ratio("1536x1024") == "3:2"
    assert providers._size_to_aspect_ratio("1920x1080") == "16:9"
    assert providers._size_to_aspect_ratio("2048x1024") == "2:1"
    assert providers._size_to_aspect_ratio("3:2") == "3:2"
    assert providers._size_to_aspect_ratio("auto") == "auto"
    assert providers._size_to_resolution("1366x768") == "1k"
    assert providers._size_to_resolution("1024x1536") == "2k"
    assert providers._size_to_resolution("16:9") is None


@pytest.mark.asyncio
async def test_generate_image_grok_uses_default_model_and_rejects_untrusted_base(
    monkeypatch, tmp_path
):
    from types import SimpleNamespace

    from app.services.agent_tool_exec import images

    calls = []

    async def grok(*args, **kwargs):
        calls.append((args, kwargs))
        return b"grok-bytes"

    monkeypatch.setattr(agent_tools, "_generate_image_grok", grok)
    monkeypatch.setattr(
        agent_tools,
        "_get_tool_config",
        AsyncMock(return_value={"api_key": "key"}),
    )
    monkeypatch.setattr("app.config.get_settings", lambda: SimpleNamespace(XAI_API_KEY=""))

    result = await images._generate_image(
        uuid.uuid4(),
        tmp_path,
        {"prompt": "paint a tree", "save_path": "workspace/images/tree.png"},
        "grok",
    )

    assert calls == [(("key", "grok-imagine-image-2.0", "https://api.x.ai/v1", "paint a tree", "1024x1024"), {})]
    assert "Provider: grok | Model: grok-imagine-image-2.0" in result

    monkeypatch.setattr(
        agent_tools,
        "_get_tool_config",
        AsyncMock(return_value={"api_key": "key", "base_url": "https://evil.test/v1"}),
    )
    denied = await images._generate_image(
        uuid.uuid4(),
        tmp_path,
        {"prompt": "paint a tree", "save_path": "workspace/images/tree.png"},
        "grok",
    )
    assert denied == "❌ xAI base_url must be https://api.x.ai/v1"


@pytest.mark.asyncio
async def test_generate_image_grok_falls_back_to_env_key(monkeypatch, tmp_path):
    from types import SimpleNamespace

    from app.services.agent_tool_exec import images

    calls = []

    async def grok(*args, **kwargs):
        calls.append(args)
        return b"env-bytes"

    monkeypatch.setattr(agent_tools, "_generate_image_grok", grok)
    monkeypatch.setattr(agent_tools, "_get_tool_config", AsyncMock(return_value={}))
    monkeypatch.setattr("app.config.get_settings", lambda: SimpleNamespace(XAI_API_KEY="env-key"))

    result = await images._generate_image(
        uuid.uuid4(),
        tmp_path,
        {"prompt": "paint a tree", "save_path": "workspace/images/tree.png"},
        "grok",
    )

    assert calls[0][0] == "env-key"
    assert "Provider: grok" in result


@pytest.mark.asyncio
async def test_provider_api_error_preserves_error_message(monkeypatch):
    install_fake_httpx(monkeypatch, [FakeResponse(429, {"message": "rate limited"}, text="fallback")])

    with pytest.raises(ValueError, match=r"SiliconFlow API error \(429\): rate limited"):
        await agent_tools._generate_image_siliconflow("key", "flux", "https://api.test", "draw", "1024x1024")


@pytest.mark.asyncio
async def test_generate_image_uses_facade_config_and_writes_provider_bytes(monkeypatch, tmp_path):
    image_bytes = b"written-image"
    encoded = base64.b64encode(image_bytes).decode()
    install_fake_httpx(monkeypatch, [FakeResponse(200, {"data": [{"b64_json": encoded}]})])
    config = AsyncMock(return_value={"api_key": "key", "model": "configured", "base_url": "https://openai.test/v1"})
    monkeypatch.setattr(agent_tools, "_get_tool_config", config)
    agent_id = uuid.uuid4()

    result = await agent_tools._generate_image(
        agent_id,
        tmp_path,
        {"prompt": "paint a tree", "save_path": "workspace/images/tree.png"},
        "openai",
    )

    assert (tmp_path / "workspace/images/tree.png").read_bytes() == image_bytes
    assert f"/api/agents/{agent_id}/files/download?path=workspace/images/tree.png" in result
    assert "Provider: openai | Model: configured" in result
    config.assert_awaited_once_with(agent_id, "generate_image_openai")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider", "alias_name", "expected_args", "expected_kwargs"),
    [
        (
            "siliconflow",
            "_generate_image_siliconflow",
            ("key", "configured", "https://provider.test", "paint a tree", "1024x1024"),
            {},
        ),
        (
            "openai",
            "_generate_image_openai",
            ("key", "configured", "https://provider.test", "paint a tree", "1024x1024"),
            {},
        ),
        (
            "google",
            "_generate_image_google",
            ("key", "configured", "https://provider.test", "paint a tree", "1024x1024"),
            {},
        ),
        (
            "grok",
            "_generate_image_grok",
            ("key", "configured", "https://api.x.ai/v1", "paint a tree", "1024x1024"),
            {},
        ),
        (
            "custom",
            "_generate_image_custom_api",
            (),
            {
                "api_key": "key",
                "model": "configured",
                "base_url": "https://provider.test",
                "endpoint_path": "/chat/completions",
                "request_body_template_json": "",
                "response_image_path": "choices.0.message.images.0.image_url.url",
                "extra_headers_json": "",
                "timeout_seconds": 120,
                "prompt": "paint a tree",
                "size": "1024x1024",
            },
        ),
    ],
)
async def test_generate_image_uses_post_import_facade_provider_alias(
    monkeypatch, tmp_path, provider, alias_name, expected_args, expected_kwargs
):
    install_fake_httpx(monkeypatch, [])
    config_base = "https://api.x.ai/v1" if provider == "grok" else "https://provider.test"
    monkeypatch.setattr(
        agent_tools,
        "_get_tool_config",
        AsyncMock(return_value={"api_key": "key", "model": "configured", "base_url": config_base}),
    )
    calls = []

    async def facade_provider(*args, **kwargs):
        calls.append((args, kwargs))
        return b"facade-provider-image"

    monkeypatch.setattr(agent_tools, alias_name, facade_provider)

    result = await images._generate_image(
        uuid.uuid4(),
        tmp_path,
        {"prompt": "paint a tree", "save_path": f"nested/{provider}.png"},
        provider,
    )

    assert calls == [(expected_args, expected_kwargs)]
    assert (tmp_path / f"nested/{provider}.png").read_bytes() == b"facade-provider-image"
    assert f"Provider: {provider} | Model: configured" in result


@pytest.mark.asyncio
async def test_generate_image_rejects_sibling_prefix_output_path(monkeypatch, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(
        agent_tools,
        "_get_tool_config",
        AsyncMock(return_value={"api_key": "key", "model": "configured", "base_url": "https://provider.test"}),
    )

    async def unexpected_provider(*_args, **_kwargs):
        raise AssertionError("provider should not run for an escaped output path")

    monkeypatch.setattr(agent_tools, "_generate_image_openai", unexpected_provider)

    result = await images._generate_image(
        uuid.uuid4(),
        workspace,
        {"prompt": "paint a tree", "save_path": "../workspace-escape/image.png"},
        "openai",
    )

    assert result == "❌ Access denied: save path is outside the workspace"


@pytest.mark.asyncio
async def test_generate_image_writes_allowed_nested_output_path(monkeypatch, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(
        agent_tools,
        "_get_tool_config",
        AsyncMock(return_value={"api_key": "key", "model": "configured", "base_url": "https://provider.test"}),
    )

    async def nested_provider(*_args, **_kwargs):
        return b"nested-image"

    monkeypatch.setattr(agent_tools, "_generate_image_openai", nested_provider)

    result = await images._generate_image(
        uuid.uuid4(),
        workspace,
        {"prompt": "paint a tree", "save_path": "nested/image.png"},
        "openai",
    )

    assert (workspace / "nested/image.png").read_bytes() == b"nested-image"
    assert "nested/image.png" in result


@pytest.mark.asyncio
async def test_custom_image_api_renders_template_headers_and_data_url(monkeypatch):
    image_bytes = b"custom-image"
    data_url = "data:image/png;base64," + base64.b64encode(image_bytes).decode()
    fake_httpx = install_fake_httpx(
        monkeypatch,
        [FakeResponse(200, {"choices": [{"message": {"images": [{"image_url": {"url": data_url}}]}}]})],
    )

    result = await agent_tools._generate_image_custom_api(
        "key",
        "custom-model",
        "https://custom.test/api",
        "/generate",
        '{"model":"{model}","prompt":"{prompt}","size":"{size}"}',
        "choices.0.message.images.0.image_url.url",
        '{"X-Trace":"trace-1","X-Number":7}',
        45,
        "draw a rose",
        "768x1024",
    )

    assert result == image_bytes
    assert fake_httpx.calls == [
        (
            "POST",
            "https://custom.test/api/generate",
            {"timeout": 45},
            {
                "json": {"model": "custom-model", "prompt": "draw a rose", "size": "768x1024"},
                "headers": {
                    "Authorization": "Bearer key",
                    "Content-Type": "application/json",
                    "X-Trace": "trace-1",
                    "X-Number": "7",
                },
            },
        )
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "message"),
    [
        (FakeResponse(503, {"error": {"message": "offline"}}), "Custom image API error (503): offline"),
        (FakeResponse(200, {"result": {"status": "ok"}}), "No image found in custom image API response"),
    ],
)
async def test_custom_image_api_reports_provider_and_reference_failures(monkeypatch, response, message):
    install_fake_httpx(monkeypatch, [response])

    with pytest.raises(ValueError, match=re.escape(message)):
        await agent_tools._generate_image_custom_api(
            "key", "model", "https://custom.test", "/chat/completions", "", "", "", 120, "draw", "1024x1024"
        )


@pytest.mark.asyncio
async def test_image_facades_defer_to_extracted_modules(monkeypatch, tmp_path):
    images = types.ModuleType("app.services.agent_tool_exec.images")
    providers = types.ModuleType("app.services.agent_tool_exec.images_providers")
    custom = types.ModuleType("app.services.agent_tool_exec.images_custom")
    calls = []

    def json_path_get(*args, **kwargs):
        calls.append(("json_path_get", args, kwargs))
        return "json path"

    def render_json_template(*args, **kwargs):
        calls.append(("render_json_template", args, kwargs))
        return {"rendered": True}

    def json_structure_preview(*args, **kwargs):
        calls.append(("json_structure_preview", args, kwargs))
        return {"preview": True}

    def find_first_image_reference(*args, **kwargs):
        calls.append(("find_first_image_reference", args, kwargs))
        return "image ref"

    async def upload_image(*args, **kwargs):
        calls.append(("upload_image", args, kwargs))
        return "uploaded"

    async def generate_image(*args, **kwargs):
        calls.append(("generate_image", args, kwargs))
        return "generated"

    async def siliconflow(*args, **kwargs):
        calls.append(("siliconflow", args, kwargs))
        return b"siliconflow"

    async def openai(*args, **kwargs):
        calls.append(("openai", args, kwargs))
        return b"openai"

    async def google(*args, **kwargs):
        calls.append(("google", args, kwargs))
        return b"google"

    async def grok(*args, **kwargs):
        calls.append(("grok", args, kwargs))
        return b"grok"

    async def custom_image_reference_to_bytes(*args, **kwargs):
        calls.append(("custom_image_reference_to_bytes", args, kwargs))
        return b"reference"

    async def generate_image_custom_api(*args, **kwargs):
        calls.append(("generate_image_custom_api", args, kwargs))
        return b"custom"

    for module, functions in (
        (images, {"_upload_image": upload_image, "_generate_image": generate_image}),
        (
            providers,
            {
                "_generate_image_siliconflow": siliconflow,
                "_generate_image_openai": openai,
                "_generate_image_google": google,
                "_generate_image_grok": grok,
            },
        ),
        (
            custom,
            {
                "_json_path_get": json_path_get,
                "_render_json_template": render_json_template,
                "_json_structure_preview": json_structure_preview,
                "_find_first_image_reference": find_first_image_reference,
                "_custom_image_reference_to_bytes": custom_image_reference_to_bytes,
                "_generate_image_custom_api": generate_image_custom_api,
            },
        ),
    ):
        for name, function in functions.items():
            setattr(module, name, function)
    monkeypatch.setitem(sys.modules, images.__name__, images)
    monkeypatch.setitem(sys.modules, providers.__name__, providers)
    monkeypatch.setitem(sys.modules, custom.__name__, custom)

    assert agent_tools._json_path_get({}, "path") == "json path"
    assert agent_tools._render_json_template("{}", {}) == {"rendered": True}
    assert agent_tools._json_structure_preview({}) == {"preview": True}
    assert agent_tools._find_first_image_reference({}) == "image ref"
    assert await agent_tools._custom_image_reference_to_bytes("ref", object()) == b"reference"
    assert (
        await agent_tools._generate_image_custom_api("key", "model", "base", "/path", "", "", "", 1, "prompt", "size")
        == b"custom"
    )
    assert await agent_tools._generate_image_siliconflow("key", "model", "base", "prompt", "size") == b"siliconflow"
    assert await agent_tools._generate_image_openai("key", "model", "base", "prompt", "size") == b"openai"
    assert await agent_tools._generate_image_google("key", "model", "base", "prompt", "size") == b"google"
    assert await agent_tools._generate_image_grok("key", "model", "base", "prompt", "size") == b"grok"
    assert await agent_tools._upload_image(uuid.uuid4(), tmp_path, {}) == "uploaded"
    assert await agent_tools._generate_image(uuid.uuid4(), tmp_path, {}, "openai") == "generated"
    assert [name for name, _args, _kwargs in calls] == [
        "json_path_get",
        "render_json_template",
        "json_structure_preview",
        "find_first_image_reference",
        "custom_image_reference_to_bytes",
        "generate_image_custom_api",
        "siliconflow",
        "openai",
        "google",
        "grok",
        "upload_image",
        "generate_image",
    ]
