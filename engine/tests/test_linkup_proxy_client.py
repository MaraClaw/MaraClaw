"""Proxy client rotation and async job binding."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from app.records.linkup_api_key import LinkupApiKeyRecord
from app.services.linkup.client import LinkupProxyError, allowed_upstream_path, proxy_linkup
from app.services.linkup.jobs import LinkupJobKeyRemovedError, bind_job, key_for_job
from app.services.linkup.keys import add_key
from app.services.linkup.tokens import make_proxy_token, parse_proxy_token


class MemoryKeyDao:
    def __init__(self) -> None:
        self.rows: dict[UUID, LinkupApiKeyRecord] = {}
        self.cursor: UUID | None = None

    async def get(self, key_id: UUID) -> LinkupApiKeyRecord | None:
        return self.rows.get(key_id)

    async def get_by_fingerprint(self, fingerprint: str) -> LinkupApiKeyRecord | None:
        for row in self.rows.values():
            if row.key_fingerprint == fingerprint:
                return row
        return None

    async def is_empty(self) -> bool:
        return not self.rows

    async def max_position(self) -> int | None:
        if not self.rows:
            return None
        return max(row.position for row in self.rows.values())

    async def list_ordered(self) -> list[LinkupApiKeyRecord]:
        return sorted(self.rows.values(), key=lambda row: row.position)

    async def list_active_ordered(self, *, now) -> list[LinkupApiKeyRecord]:
        del now
        return sorted(
            [row for row in self.rows.values() if row.status == "active"],
            key=lambda row: row.position,
        )

    async def create(self, *, obj_in: dict) -> LinkupApiKeyRecord:
        record = LinkupApiKeyRecord(
            id=uuid4(),
            label=str(obj_in["label"]),
            key_ciphertext=str(obj_in["key_ciphertext"]),
            key_fingerprint=str(obj_in["key_fingerprint"]),
            position=int(obj_in["position"]),
            status="active",
        )
        self.rows[record.id] = record
        return record

    async def update(self, *, db_obj: LinkupApiKeyRecord, obj_in: dict) -> LinkupApiKeyRecord:
        for key, value in obj_in.items():
            setattr(db_obj, key, value)
        return db_obj

    async def delete(self, *, id: UUID) -> LinkupApiKeyRecord | None:
        return self.rows.pop(id, None)

    async def get_cursor_key_id(self) -> UUID | None:
        return self.cursor

    async def set_cursor_key_id(self, key_id: UUID | None) -> None:
        self.cursor = key_id


class MemoryJobDao:
    def __init__(self) -> None:
        self.rows: dict[str, object] = {}

    async def get_by_job_id(self, upstream_job_id: str):
        return self.rows.get(upstream_job_id)

    async def create(self, *, obj_in: dict):
        from app.records.linkup_api_key import LinkupAsyncJobRecord

        record = LinkupAsyncJobRecord(
            upstream_job_id=str(obj_in["upstream_job_id"]),
            key_id=obj_in["key_id"],
            kind=str(obj_in["kind"]),
        )
        self.rows[record.upstream_job_id] = record
        return record


class FakeResponse:
    def __init__(self, status_code: int, text: str) -> None:
        self.status_code = status_code
        self.text = text
        self.headers: dict[str, str] = {"content-type": "application/json"}


class FakeClient:
    def __init__(self, responses: list[FakeResponse], calls: list[tuple[str, str, str]]) -> None:
        self._responses = responses
        self._calls = calls

    async def __aenter__(self) -> FakeClient:
        return self

    async def __aexit__(self, *_args) -> bool:
        return False

    async def request(self, method: str, url: str, headers: dict[str, str], content: bytes | None):
        del content
        auth = headers.get("Authorization", "")
        self._calls.append((method, url, auth))
        return self._responses.pop(0)


@pytest.fixture
def ring(monkeypatch: pytest.MonkeyPatch) -> MemoryKeyDao:
    from app.services.linkup import client as client_mod
    from app.services.linkup import jobs as jobs_mod
    from app.services.linkup import keys as keys_mod

    store = MemoryKeyDao()
    jobs = MemoryJobDao()
    settings = SimpleNamespace(SECRET_KEY="test-secret", LINKUP_API_KEY="")
    monkeypatch.setattr(keys_mod, "linkup_api_key_dao", store)
    monkeypatch.setattr(keys_mod, "get_settings", lambda: settings)
    monkeypatch.setattr(jobs_mod, "linkup_api_key_dao", store)
    monkeypatch.setattr(jobs_mod, "linkup_async_job_dao", jobs)
    monkeypatch.setattr(client_mod, "get_settings", lambda: settings)

    async def _passthrough_record(
        result: tuple[int, str, dict[str, str]], **_kwargs: object
    ) -> tuple[int, str, dict[str, str]]:
        return result

    monkeypatch.setattr(client_mod, "_record_result", _passthrough_record)
    return store


@pytest.mark.asyncio
async def test_proxy_rotates_on_quota_and_succeeds_on_next_key(
    ring: MemoryKeyDao, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services.linkup import client as client_mod
    from app.core.security import decrypt_data

    first = await add_key(label="a", api_key="key-a")
    second = await add_key(label="b", api_key="key-b")
    calls: list[tuple[str, str, str]] = []
    responses = [
        FakeResponse(429, '{"error":"insufficient quota"}'),
        FakeResponse(200, '{"ok":true}'),
    ]
    monkeypatch.setattr(
        client_mod,
        "_httpx_client",
        lambda *a, **k: FakeClient(responses, calls),
    )

    status, body, _headers = await proxy_linkup(
        method="POST",
        path="search",
        headers={},
        content=b'{"q":"x"}',
    )
    assert status == 200
    assert body == '{"ok":true}'
    assert decrypt_data(first.key_ciphertext, "test-secret") == "key-a"
    assert "Bearer key-a" in calls[0][2]
    assert "Bearer key-b" in calls[1][2]
    assert first.exhausted_until is not None
    assert ring.cursor == second.id


@pytest.mark.asyncio
async def test_proxy_does_not_rotate_on_500_then_retries_same_key(
    ring: MemoryKeyDao, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services.linkup import client as client_mod

    _ = ring
    _ = await add_key(label="a", api_key="key-a")
    _ = await add_key(label="b", api_key="key-b")
    calls: list[tuple[str, str, str]] = []
    responses = [
        FakeResponse(500, "boom"),
        FakeResponse(200, '{"ok":true}'),
    ]
    monkeypatch.setattr(client_mod, "_httpx_client", lambda *a, **k: FakeClient(responses, calls))
    status, body, _headers = await proxy_linkup(method="POST", path="search", headers={}, content=b"{}")
    assert status == 200
    assert body == '{"ok":true}'
    assert [auth for _m, _u, auth in calls] == ["Bearer key-a", "Bearer key-a"]


@pytest.mark.asyncio
async def test_research_post_and_get_use_same_bound_key(
    ring: MemoryKeyDao, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services.linkup import client as client_mod

    first = await add_key(label="a", api_key="key-a")
    _ = await add_key(label="b", api_key="key-b")
    calls: list[tuple[str, str, str]] = []
    responses = [
        FakeResponse(200, '{"id":"job-1","status":"pending"}'),
        FakeResponse(200, '{"id":"job-1","status":"completed"}'),
    ]
    monkeypatch.setattr(client_mod, "_httpx_client", lambda *a, **k: FakeClient(responses, calls))
    status, body, _h = await proxy_linkup(method="POST", path="research", headers={}, content=b"{}")
    assert status == 200
    assert "job-1" in body
    bound = await key_for_job("job-1")
    assert bound.id == first.id

    status, _body, _h = await proxy_linkup(method="GET", path="research/job-1", headers={}, content=None)
    assert status == 200
    assert calls[1][2] == "Bearer key-a"


@pytest.mark.asyncio
async def test_get_after_bound_key_removed_fails_closed(ring: MemoryKeyDao) -> None:
    first = await add_key(label="a", api_key="key-a")
    await bind_job(upstream_job_id="job-9", key_id=first.id, kind="research")
    _ = await remove_key_safe(first.id)
    with pytest.raises(LinkupJobKeyRemovedError):
        _ = await key_for_job("job-9")
    with pytest.raises(LinkupProxyError) as exc:
        await proxy_linkup(method="GET", path="research/job-9", headers={}, content=None)
    assert exc.value.status_code == 410


async def remove_key_safe(key_id: UUID):
    from app.services.linkup.keys import remove_key

    return await remove_key(key_id)


@pytest.mark.asyncio
async def test_empty_ring_returns_503(ring: MemoryKeyDao) -> None:
    _ = ring
    with pytest.raises(LinkupProxyError) as exc:
        await proxy_linkup(method="POST", path="search", headers={}, content=b"{}")
    assert exc.value.status_code == 503


def test_path_allowlist_and_proxy_token() -> None:
    assert allowed_upstream_path("search") is True
    assert allowed_upstream_path("research/abc") is True
    assert allowed_upstream_path("admin") is False
    agent_id = uuid4()
    token = make_proxy_token(agent_id, secret="s")
    assert parse_proxy_token(token, secret="s") == agent_id
    assert parse_proxy_token(token, secret="other") is None
    assert parse_proxy_token("not-a-token", secret="s") is None
