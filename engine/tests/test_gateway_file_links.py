"""POST /api/gateway/file-links: path rules, contained local reads, durable-store sync outcomes and the link shape."""

import os
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient, Response
from redis.exceptions import RedisError

from app.api import gateway
from app.config import Settings, get_settings
from app.core.logging import configure_logging
from app.records.agent import AgentRecord
from app.services import file_share_links as links, file_share_tokens as shares, platform_service as platform_module
from app.services.workspace_mirror import MirrorBusyError, MirrorConflictError, SyncedFile

API_KEY = "gwk_test_key"
BASE_URL = "https://maraclaw.test"
CONFLICT_PATH = "workspace/conflicts/report (conflict 2026-11-19 101500).pdf"


class ShareRedis:
    def __init__(self) -> None:
        self.values: dict[str, tuple[str, int]] = {}

    async def set(self, key: str, value: str, *, nx: bool, ex: int) -> bool | None:
        assert nx
        if key in self.values:
            return None
        self.values[key] = (value, ex)
        return True

    async def get(self, key: str) -> str | None:
        value = self.values.get(key)
        return value[0] if value else None


@dataclass(frozen=True, slots=True)
class Env:
    agent: AgentRecord
    state: Path
    redis: ShareRedis


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Env:
    redis = ShareRedis()
    monkeypatch.setattr(shares, "get_redis", AsyncMock(return_value=redis))
    agent = AgentRecord(id=uuid.uuid4(), name="Mara", creator_id=uuid.uuid4())
    state = tmp_path / str(agent.id)
    (state / "workspace").mkdir(parents=True)
    monkeypatch.setattr(links, "agent_state_dir", lambda _agent_id: state)
    monkeypatch.setattr(links, "durable_store", lambda: None)
    monkeypatch.setattr(platform_module, "get_settings", lambda: Settings(PUBLIC_BASE_URL=BASE_URL))

    async def by_key(api_key: str) -> AgentRecord | None:
        return agent if api_key == API_KEY else None

    monkeypatch.setattr(gateway.agent_dao, "get_openclaw_by_api_key", by_key)
    return Env(agent, state, redis)


@pytest.fixture
def durable(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """S3 mode: a durable store exists, and the single-path sync is the test's to script."""
    monkeypatch.setattr(links, "durable_store", lambda: object())
    sync = AsyncMock()
    monkeypatch.setattr(links, "sync_path", sync)
    return sync


@pytest.fixture
def app_log() -> Iterator[list[str]]:
    chunks: list[str] = []
    configure_logging(level="DEBUG", fmt="text", enqueue=False, color=False, sink=chunks.append)
    yield chunks
    configure_logging()


async def post_json(body: object, *, key: str | None = API_KEY) -> Response:
    from app.main import app

    headers = {} if key is None else {"X-Api-Key": key}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return await client.post("/api/gateway/file-links", headers=headers, json=body)


async def post_link(path: str) -> Response:
    return await post_json({"path": path})


def write(env: Env, rel: str, data: bytes = b"data") -> Path:
    target: Path = env.state / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    _ = target.write_bytes(data)
    return target


def assert_refused(response: Response, status: int, code: str, env: Env) -> dict[str, object]:
    assert response.status_code == status
    detail = cast("dict[str, object]", response.json()["detail"])
    assert detail["code"] == code
    assert env.redis.values == {}, "a refused request must not leave a token behind"
    return detail


async def test_missing_or_unknown_api_key_is_unauthorized(env: Env) -> None:
    assert (await post_json({"path": "workspace/a.txt"}, key=None)).status_code == 401
    unknown = await post_json({"path": "workspace/a.txt"}, key="nope")
    assert unknown.status_code == 401
    assert unknown.json() == {"detail": "Invalid API key"}
    assert env.redis.values == {}


@pytest.mark.parametrize(
    "body",
    [{}, {"path": ""}, {"path": 3}, {"path": "x" * 1025}, {"path": "workspace/a.txt", "inline": True}],
)
async def test_malformed_request_is_rejected_before_any_lookup(env: Env, body: object) -> None:
    assert (await post_json(body)).status_code == 422
    assert env.redis.values == {}


@pytest.mark.parametrize("path", ["../x", "workspace/../../x", "/etc/passwd", "workspace/a\x00b", ".", "./"])
async def test_path_outside_the_agent_directory_is_invalid(env: Env, path: str) -> None:
    assert_refused(await post_link(path), 400, "invalid_path", env)


@pytest.mark.parametrize(
    "path",
    [
        "openclaw.json",
        "memory/x.md",
        "soul.md",
        ".maraclaw-gateway-key",
        "workspace/.env",
        "workspace/node_modules/x.js",
        "workspace/state.db",
        "workspace",
    ],
)
async def test_file_outside_the_shareable_tree_is_refused_even_when_it_exists(env: Env, path: str) -> None:
    if path != "workspace":
        _ = write(env, path)
    assert_refused(await post_link(path), 400, "path_not_shareable", env)


async def test_missing_file_is_404(env: Env) -> None:
    assert_refused(await post_link("workspace/nope.txt"), 404, "file_not_found", env)
    assert_refused(await post_link("workspace/missing-dir/nope.txt"), 404, "file_not_found", env)


def _symlinked_file(env: Env) -> str:
    target = write(env, "memory/target.txt")
    (env.state / "workspace/link.txt").symlink_to(target)
    return "workspace/link.txt"


def _symlinked_directory(env: Env) -> str:
    _ = write(env, "memory/dir/x.txt")
    (env.state / "workspace/dir").symlink_to(env.state / "memory/dir")
    return "workspace/dir/x.txt"


def _directory(env: Env) -> str:
    (env.state / "workspace/sub").mkdir()
    return "workspace/sub"


def _fifo(env: Env) -> str:
    os.mkfifo(env.state / "workspace/pipe")
    return "workspace/pipe"


@pytest.mark.parametrize("build", [_symlinked_file, _symlinked_directory, _directory, _fifo])
async def test_anything_but_a_plain_file_is_404(env: Env, build: Callable[[Env], str]) -> None:
    assert_refused(await post_link(build(env)), 404, "file_not_found", env)


async def test_hard_linked_file_is_not_shareable(env: Env) -> None:
    secret = write(env, "memory/secret.txt")
    os.link(secret, env.state / "workspace/hard.txt")
    assert_refused(await post_link("workspace/hard.txt"), 400, "path_not_shareable", env)


@pytest.mark.parametrize(
    "raw",
    [
        "workspace/B\u00e1o c\u00e1o Q3.pdf",
        "/home/node/custom-state/workspace/B\u00e1o c\u00e1o Q3.pdf",
        ".openclaw/workspace/B\u00e1o c\u00e1o Q3.pdf",
    ],
)
async def test_local_mode_issues_a_link_for_the_files_own_name(env: Env, raw: str) -> None:
    _ = write(env, "workspace/B\u00e1o c\u00e1o Q3.pdf", b"12345")
    ttl = get_settings().FILE_SHARE_LINK_TTL_SECONDS
    before = datetime.now(UTC)

    response = await post_link(raw)

    after = datetime.now(UTC)
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"url", "expires_at", "expires_in_seconds", "filename", "size_bytes"}
    assert (body["filename"], body["size_bytes"], body["expires_in_seconds"]) == ("B\u00e1o c\u00e1o Q3.pdf", 5, ttl)
    assert (
        before + timedelta(seconds=ttl) <= datetime.fromisoformat(body["expires_at"]) <= after + timedelta(seconds=ttl)
    )
    prefix = f"{BASE_URL}/f/"
    assert body["url"].startswith(prefix)
    token, name = body["url"].removeprefix(prefix).split("/")
    assert name == "B%C3%A1o%20c%C3%A1o%20Q3.pdf"
    assert await shares.resolve(token) == shares.FileShareGrant(env.agent.id, "workspace/B\u00e1o c\u00e1o Q3.pdf")


async def test_outbound_media_is_shareable(env: Env) -> None:
    _ = write(env, "media/outbound/chart.png", b"png")
    response = await post_link("media/outbound/chart.png")
    assert response.status_code == 200
    assert response.json()["filename"] == "chart.png"


async def test_link_is_refused_without_a_public_base_url(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    _ = write(env, "workspace/report.pdf")
    monkeypatch.setattr(platform_module, "get_settings", lambda: Settings(PUBLIC_BASE_URL=""))
    assert_refused(await post_link("workspace/report.pdf"), 503, "public_base_url_not_configured", env)


@pytest.mark.parametrize("failure", ["connect", "command"])
async def test_link_is_refused_while_the_token_store_is_down(
    env: Env, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    _ = write(env, "workspace/report.pdf")
    if failure == "connect":
        monkeypatch.setattr(shares, "get_redis", AsyncMock(side_effect=RedisError()))
    else:
        monkeypatch.setattr(env.redis, "set", AsyncMock(side_effect=RedisError()))
    assert_refused(await post_link("workspace/report.pdf"), 503, "file_share_unavailable", env)


async def test_durable_store_mode_reports_the_synced_size(env: Env, durable: AsyncMock) -> None:
    _ = write(env, "workspace/report.pdf", b"local")
    durable.return_value = SyncedFile("workspace/report.pdf", f"{env.agent.id}/workspace/report.pdf", 4321)

    response = await post_link("workspace/report.pdf")

    assert response.status_code == 200
    assert response.json()["size_bytes"] == 4321
    durable.assert_awaited_once_with(env.agent, "workspace/report.pdf")


@pytest.mark.parametrize(
    ("failure", "status", "code", "extra"),
    [
        (MirrorBusyError(), 503, "file_sync_busy", {}),
        (
            MirrorConflictError("workspace/report.pdf", CONFLICT_PATH),
            409,
            "file_conflict",
            {"conflict_path": CONFLICT_PATH},
        ),
        (FileNotFoundError(), 404, "file_not_found", {}),
    ],
)
async def test_durable_store_sync_outcomes_map_to_coded_errors(
    env: Env,
    durable: AsyncMock,
    failure: Exception,
    status: int,
    code: str,
    extra: dict[str, str],
) -> None:
    _ = write(env, "workspace/report.pdf")
    durable.side_effect = failure
    detail = assert_refused(await post_link("workspace/report.pdf"), status, code, env)
    assert detail.items() >= extra.items()


async def test_durable_store_mode_without_a_public_base_url_syncs_nothing(
    env: Env, durable: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ = write(env, "workspace/report.pdf")
    monkeypatch.setattr(platform_module, "get_settings", lambda: Settings(PUBLIC_BASE_URL=""))
    assert_refused(await post_link("workspace/report.pdf"), 503, "public_base_url_not_configured", env)
    durable.assert_not_awaited()


async def test_audit_line_names_the_agent_and_file_but_never_the_token_or_url(env: Env, app_log: list[str]) -> None:
    _ = write(env, "workspace/report.pdf", b"12345")

    response = await post_link("workspace/report.pdf")

    url = response.json()["url"]
    token = url.split("/")[-2]
    output = "\n".join(line for line in "".join(app_log).splitlines() if " | app." in line)
    assert f"[FileShare] link issued agent={env.agent.id} path=workspace/report.pdf size=5 ttl=" in output
    assert token not in output
    assert url not in output
