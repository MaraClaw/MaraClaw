import asyncio

import httpx
import pytest

from app.core.json_types import JsonObject
from app.services import feishu_service as feishu_service_module
from app.services.feishu_service import FeishuAPIError
from im_token_cache_fakes import install_token_cache


@pytest.fixture(autouse=True)
def _token_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    _ = install_token_cache(monkeypatch)


class _FakeResponse:
    def __init__(self, status_code: int, payload: JsonObject):
        self.status_code = status_code
        self._payload = payload

    def json(self) -> JsonObject:
        return self._payload


class _FakeAsyncClient:
    def __init__(self, *, send_payload: JsonObject | None = None, patch_payload: JsonObject | None = None):
        self._send_payload = send_payload or {"code": 0, "msg": "ok", "data": {"message_id": "m_1"}}
        self._patch_payload = patch_payload or {"code": 0, "msg": "ok"}

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, url, **_kwargs):
        if "app_access_token/internal" in url:
            return _FakeResponse(200, {"app_access_token": "token_x"})
        return _FakeResponse(200, self._send_payload)

    async def patch(self, _url, **_kwargs):
        return _FakeResponse(200, self._patch_payload)


class _FakeResult:
    def __init__(self, value):
        self._value = value

    def scalars(self):
        return self

    def first(self):
        return self._value


@pytest.mark.asyncio
async def test_send_message_raises_when_business_code_nonzero(monkeypatch):
    monkeypatch.setattr(
        feishu_service_module.httpx,
        "AsyncClient",
        lambda: _FakeAsyncClient(send_payload={"code": 99991663, "msg": "rate limited"}),
    )

    with pytest.raises(RuntimeError, match="code=99991663"):
        await feishu_service_module.feishu_service.send_message(
            "app_id",
            "app_secret",
            "ou_xxx",
            "text",
            '{"text":"hello"}',
            stage="unit_test_send",
        )


@pytest.mark.asyncio
async def test_patch_message_raises_when_business_code_nonzero(monkeypatch):
    monkeypatch.setattr(
        feishu_service_module.httpx,
        "AsyncClient",
        lambda: _FakeAsyncClient(patch_payload={"code": 10019, "msg": "invalid card content"}),
    )

    with pytest.raises(RuntimeError, match="code=10019"):
        await feishu_service_module.feishu_service.patch_message(
            "app_id",
            "app_secret",
            "om_xxx",
            '{"content":"test"}',
            stage="unit_test_patch",
        )


def _mock_feishu(monkeypatch: pytest.MonkeyPatch, token_body: JsonObject) -> list[httpx.Request]:
    requests: list[httpx.Request] = []
    original = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if "app_access_token" in request.url.path:
            return httpx.Response(200, json=token_body)
        return httpx.Response(200, json={"code": 0, "data": {"message_id": "om_1"}})

    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs)
    )
    return requests


async def test_concurrent_calls_for_one_app_mint_a_single_token(monkeypatch):
    requests = _mock_feishu(monkeypatch, {"code": 0, "app_access_token": "tok", "expire": 7200})
    service = feishu_service_module.feishu_service

    await asyncio.gather(
        *(service.patch_message("app", "secret", f"om_{index}", "{}") for index in range(3)),
        service.send_message("app", "secret", "ou_1", "text", "{}"),
    )

    assert sum("app_access_token" in request.url.path for request in requests) == 1
    assert {request.headers["Authorization"] for request in requests if "app_access_token" not in request.url.path} == {
        "Bearer tok"
    }


async def test_strict_send_raises_when_feishu_refuses_the_token(monkeypatch):
    requests = _mock_feishu(monkeypatch, {"code": 10003, "msg": "invalid param"})

    with pytest.raises(FeishuAPIError):
        await feishu_service_module.feishu_service.send_message("app", "secret", "ou_1", "text", "{}", strict=True)

    assert [request.url.path for request in requests] == ["/open-apis/auth/v3/app_access_token/internal"]
