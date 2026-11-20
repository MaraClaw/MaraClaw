"""GET and HEAD /f/{token}/{filename}: live-token checks, contained local streaming, durable-store redirect and stream."""

import io
import os
import uuid
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from unittest.mock import AsyncMock
from urllib.parse import quote

import pytest
from httpx import ASGITransport, AsyncClient, Response
from redis.exceptions import RedisError

from app.services import file_share_links as links, file_share_tokens as shares
from app.services.storage_runtime import file_stream
from app.services.storage_runtime.base import MaterializedFile, StorageVersion

TTL_SECONDS = 900
GONE = {"detail": "Not found"}
PAYLOAD = bytes(range(256)) * 10_240  # 2.5 MiB: more than one streamed chunk
PRESENT = StorageVersion(key="object", exists=True, is_dir=False, size=len(PAYLOAD))
ABSENT = StorageVersion(key="object", exists=False, is_dir=False)
A_DIRECTORY = StorageVersion(key="object", exists=True, is_dir=True)


class ShareRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    async def set(self, key: str, value: str, *, nx: bool, ex: int) -> bool | None:
        assert nx
        if key in self.values:
            return None
        self.values[key] = value
        return True

    async def get(self, key: str) -> str | None:
        return self.values.get(key)


class FakeStore:
    """The slice of a durable store the download route uses: a scripted version, presign and materialize."""

    def __init__(
        self,
        presigned: str | None = None,
        materialized: MaterializedFile | None = None,
        error: Exception | None = None,
        version: StorageVersion = PRESENT,
    ) -> None:
        self.presigned = presigned
        self.materialized = materialized
        self.error = error
        self.version = version
        self.version_calls: list[str] = []
        self.presign_calls: list[tuple[str, str | None, bool, int | None]] = []
        self.materialize_calls: list[str] = []

    async def get_version(self, key: str) -> StorageVersion:
        self.version_calls.append(key)
        return self.version

    async def presign_download_url(
        self, key: str, filename: str | None = None, inline: bool = False, expires_in_seconds: int | None = None
    ) -> str | None:
        self.presign_calls.append((key, filename, inline, expires_in_seconds))
        return self.presigned

    async def materialize(self, key: str) -> MaterializedFile | None:
        self.materialize_calls.append(key)
        if self.error is not None:
            raise self.error
        return self.materialized


@dataclass(frozen=True, slots=True)
class Env:
    agent_id: uuid.UUID
    state: Path
    scratch: Path


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Env:
    monkeypatch.setattr(shares, "get_redis", AsyncMock(return_value=ShareRedis()))
    agent_id = uuid.uuid4()
    state = tmp_path / str(agent_id)
    (state / "workspace").mkdir(parents=True)
    monkeypatch.setattr(links, "agent_state_dir", lambda _agent_id: state)
    monkeypatch.setattr(links, "durable_store", lambda: None)
    monkeypatch.setattr(links.agent_dao, "is_openclaw_active", AsyncMock(return_value=True))
    monkeypatch.setattr(links.company_transfer_cleanup_dao, "is_pending", AsyncMock(return_value=False))
    return Env(agent_id, state, tmp_path)


def write(env: Env, rel: str, data: bytes = PAYLOAD) -> Path:
    target: Path = env.state / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    _ = target.write_bytes(data)
    return target


async def share(env: Env, rel: str) -> str:
    token, _expires = await shares.issue(env.agent_id, rel, TTL_SECONDS)
    return token


async def fetch(token: str, name: str, method: str = "GET") -> Response:
    from app.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return await client.request(method, f"/f/{token}/{quote(name, safe='')}")


def open_descriptors() -> set[str]:
    return {entry.name for entry in Path("/dev/fd").iterdir()}


def assert_gone(response: Response) -> None:
    assert response.status_code == 404
    assert response.json() == GONE


def assert_download_headers(response: Response, *, disposition: str, content_type: str) -> None:
    assert response.headers["content-disposition"] == disposition
    assert response.headers["content-type"] == content_type
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["content-security-policy"] == "sandbox"
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["referrer-policy"] == "no-referrer"


REPORT_DISPOSITION = "attachment; filename=\"Q3 report.pdf\"; filename*=UTF-8''Q3%20report.pdf"


async def test_local_file_streams_every_byte_with_download_headers(env: Env) -> None:
    _ = write(env, "workspace/Q3 report.pdf")
    token = await share(env, "workspace/Q3 report.pdf")

    response = await fetch(token, "Q3 report.pdf")

    assert response.status_code == 200
    assert response.content == PAYLOAD
    assert response.headers["content-length"] == str(len(PAYLOAD))
    assert_download_headers(response, disposition=REPORT_DISPOSITION, content_type="application/pdf")


async def test_head_announces_the_file_without_a_body(env: Env) -> None:
    _ = write(env, "workspace/Q3 report.pdf")
    token = await share(env, "workspace/Q3 report.pdf")

    response = await fetch(token, "Q3 report.pdf", "HEAD")

    assert response.status_code == 200
    assert response.content == b""
    assert response.headers["content-length"] == str(len(PAYLOAD))
    assert_download_headers(response, disposition=REPORT_DISPOSITION, content_type="application/pdf")


@pytest.mark.parametrize("method", ["GET", "HEAD"])
async def test_serving_a_file_leaves_no_descriptor_open(env: Env, method: str) -> None:
    _ = write(env, "workspace/Q3 report.pdf")
    token = await share(env, "workspace/Q3 report.pdf")
    _ = await fetch(token, "Q3 report.pdf", method)  # warm up the lazily created loop and thread resources
    before = open_descriptors()

    _ = await fetch(token, "Q3 report.pdf", method)

    assert open_descriptors() <= before


def test_stream_stops_at_the_announced_size_and_closes_the_file(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(file_stream, "_CHUNK_BYTES", 3)
    stream = io.BytesIO(b"abcdefgh")

    assert list(file_stream.stream_chunks(stream, 5)) == [b"abc", b"de"]
    assert stream.closed


async def test_unknown_token_is_gone(env: Env) -> None:
    assert_gone(await fetch("never-issued", "report.pdf"))


async def test_other_file_name_is_gone(env: Env) -> None:
    _ = write(env, "workspace/report.pdf")
    token = await share(env, "workspace/report.pdf")
    assert_gone(await fetch(token, "other.pdf"))
    assert_gone(await fetch(token, "report.pdf.exe"))


@pytest.mark.parametrize("backend", ["local", "store"])
@pytest.mark.parametrize(
    ("owner", "check", "refusal"),
    [(links.agent_dao, "is_openclaw_active", False), (links.company_transfer_cleanup_dao, "is_pending", True)],
    ids=["agent-no-longer-gateway-active", "company-transfer-cleanup-pending"],
)
async def test_link_dies_with_the_agents_access(
    env: Env, monkeypatch: pytest.MonkeyPatch, owner: object, check: str, refusal: bool, backend: str
) -> None:
    """A deleted, stopped or expired agent, a disabled company and a transfer cleanup each end the link at once."""
    store = FakeStore(presigned="https://s3.example/bucket/key?sig=1")
    if backend == "store":
        monkeypatch.setattr(links, "durable_store", lambda: store)
    _ = write(env, "workspace/report.pdf")
    token = await share(env, "workspace/report.pdf")
    assert (await fetch(token, "report.pdf")).status_code in {200, 302}, "the link works while the agent has access"
    storage_calls = (list(store.version_calls), list(store.presign_calls))

    monkeypatch.setattr(owner, check, AsyncMock(return_value=refusal))

    assert_gone(await fetch(token, "report.pdf"))
    assert (await fetch(token, "report.pdf", "HEAD")).status_code == 404
    assert (store.version_calls, store.presign_calls) == storage_calls, "the store is not asked once access is gone"


async def test_grant_outside_the_shareable_tree_is_gone_even_when_the_file_exists(env: Env) -> None:
    _ = write(env, "openclaw.json")
    token = await share(env, "openclaw.json")
    assert_gone(await fetch(token, "openclaw.json"))


def _replace_with_symlink(env: Env, path: Path) -> None:
    target = write(env, "memory/target.bin")
    path.unlink()
    path.symlink_to(target)


def _hard_link(env: Env, path: Path) -> None:
    os.link(path, env.state / "memory/alias.bin")


def _replace_with_directory(env: Env, path: Path) -> None:
    path.unlink()
    path.mkdir()


def _delete(env: Env, path: Path) -> None:
    path.unlink()


@pytest.mark.parametrize("change", [_replace_with_symlink, _hard_link, _replace_with_directory, _delete])
async def test_file_that_stops_being_a_plain_file_after_issue_is_gone(
    env: Env, change: Callable[[Env, Path], None]
) -> None:
    (env.state / "memory").mkdir()
    path = write(env, "workspace/report.pdf")
    token = await share(env, "workspace/report.pdf")

    change(env, path)

    assert_gone(await fetch(token, "report.pdf"))


async def test_parent_directory_swapped_for_a_symlink_after_issue_is_gone(env: Env) -> None:
    _ = write(env, "workspace/sub/report.pdf")
    token = await share(env, "workspace/sub/report.pdf")
    _ = write(env, "memory/sub/report.pdf", b"someone else's bytes")
    (env.state / "workspace/sub/report.pdf").unlink()
    (env.state / "workspace/sub").rmdir()
    (env.state / "workspace/sub").symlink_to(env.state / "memory/sub")

    assert_gone(await fetch(token, "report.pdf"))


@pytest.mark.parametrize("presigned", ["https://s3.example/bucket/key?sig=1", "http://127.0.0.1:9000/bucket/key?sig=1"])
async def test_absolute_presigned_url_redirects_for_one_minute(
    env: Env, monkeypatch: pytest.MonkeyPatch, presigned: str
) -> None:
    store = FakeStore(presigned=presigned)
    monkeypatch.setattr(links, "durable_store", lambda: store)
    token = await share(env, "workspace/Q3 report.pdf")

    response = await fetch(token, "Q3 report.pdf")

    assert response.status_code == 302
    assert response.headers["location"] == presigned
    assert response.headers["cache-control"] == "private, no-store"
    assert store.version_calls == [f"{env.agent_id}/workspace/Q3 report.pdf"]
    assert store.presign_calls == [(f"{env.agent_id}/workspace/Q3 report.pdf", "Q3 report.pdf", False, 60)]


@pytest.mark.parametrize("version", [ABSENT, A_DIRECTORY], ids=["missing-object", "directory-marker"])
async def test_object_missing_from_the_store_is_gone_instead_of_redirected(
    env: Env, monkeypatch: pytest.MonkeyPatch, version: StorageVersion
) -> None:
    store = FakeStore(presigned="https://s3.example/bucket/key?sig=1", version=version)
    monkeypatch.setattr(links, "durable_store", lambda: store)
    token = await share(env, "workspace/report.pdf")

    assert_gone(await fetch(token, "report.pdf"))
    assert (await fetch(token, "report.pdf", "HEAD")).status_code == 404
    assert store.presign_calls == []


async def test_head_is_answered_from_the_stored_size_without_a_redirect(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = FakeStore(presigned="https://s3.example/bucket/key?sig=1", version=replace(PRESENT, size=1234))
    monkeypatch.setattr(links, "durable_store", lambda: store)
    token = await share(env, "workspace/Q3 report.pdf")

    response = await fetch(token, "Q3 report.pdf", "HEAD")

    assert response.status_code == 200
    assert response.content == b""
    assert response.headers["content-length"] == "1234"
    assert "location" not in response.headers
    assert_download_headers(response, disposition=REPORT_DISPOSITION, content_type="application/pdf")
    assert store.version_calls == [f"{env.agent_id}/workspace/Q3 report.pdf"]
    assert store.presign_calls == []
    assert store.materialize_calls == []


@pytest.mark.parametrize("presigned", ["/minio/bucket/key?sig=1", None])
@pytest.mark.parametrize("owned", [True, False])
async def test_object_without_an_absolute_presigned_url_is_streamed_from_the_store(
    env: Env, monkeypatch: pytest.MonkeyPatch, presigned: str | None, owned: bool
) -> None:
    copy = env.scratch / "copy.bin"
    _ = copy.write_bytes(PAYLOAD)
    monkeypatch.setattr(links, "durable_store", lambda: FakeStore(presigned, MaterializedFile(copy, owned=owned)))
    token = await share(env, "workspace/Q3 report.pdf")

    response = await fetch(token, "Q3 report.pdf")

    assert response.status_code == 200
    assert response.content == PAYLOAD
    assert response.headers["content-length"] == str(len(PAYLOAD))
    assert_download_headers(response, disposition=REPORT_DISPOSITION, content_type="application/pdf")
    assert copy.exists() is not owned, "only a temporary copy made for this request is removed"


@pytest.mark.parametrize(
    "store",
    [FakeStore("/rel", error=FileNotFoundError()), FakeStore("/rel", materialized=None)],
    ids=["missing-object", "cannot-materialize"],
)
async def test_object_the_store_cannot_provide_is_gone(
    env: Env, monkeypatch: pytest.MonkeyPatch, store: FakeStore
) -> None:
    monkeypatch.setattr(links, "durable_store", lambda: store)
    token = await share(env, "workspace/report.pdf")
    assert_gone(await fetch(token, "report.pdf"))


async def test_token_store_outage_is_unavailable_not_gone(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shares, "get_redis", AsyncMock(side_effect=RedisError()))

    response = await fetch("any-token", "report.pdf")

    assert response.status_code == 503
    assert response.json() == {"detail": "File sharing is temporarily unavailable"}
