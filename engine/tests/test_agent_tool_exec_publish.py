from __future__ import annotations

import secrets
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services.agent_tool_exec import publish
from app.services.agent_tool_exec.registry import ToolArguments


class FakeStorage:
    def __init__(self, files: dict[str, str]) -> None:
        self.files = files
        self.keys: list[str] = []

    async def exists(self, key: str) -> bool:
        self.keys.append(key)
        return key in self.files

    async def is_file(self, key: str) -> bool:
        self.keys.append(key)
        return key in self.files

    async def read_text(self, key: str, **_: object) -> str:
        self.keys.append(key)
        return self.files[key]


def patch_publish(
    monkeypatch: pytest.MonkeyPatch,
    storage: FakeStorage,
    *,
    public_base: str = "https://pages.test",
    agent: object | None = None,
    create: AsyncMock | None = None,
    list_pages: AsyncMock | None = None,
) -> AsyncMock:
    from app import config

    monkeypatch.setattr(publish, "get_storage_backend", lambda: storage)
    monkeypatch.setattr(config, "get_settings", lambda: SimpleNamespace(PUBLIC_BASE_URL=public_base))
    monkeypatch.setattr(secrets, "token_urlsafe", lambda _: "short-id-value")
    get_agent = AsyncMock(return_value=agent)
    create_mock = create or AsyncMock(return_value=SimpleNamespace(short_id="short-id"))
    list_mock = list_pages or AsyncMock(return_value=[])
    monkeypatch.setattr(publish.agent_dao, "get", get_agent)
    monkeypatch.setattr(publish.published_page_dao, "create", create_mock)
    monkeypatch.setattr(publish.published_page_dao, "list_for_agent", list_mock)
    return create_mock


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        ({}, "Missing required argument 'path'"),
        ({"path": "workspace/page.txt"}, "Only .html and .htm files can be published"),
    ],
)
async def test_publish_validates_required_html_path(arguments: ToolArguments, expected: str) -> None:
    assert await publish._publish_page(uuid.uuid4(), uuid.uuid4(), Path("unused"), arguments) == expected


@pytest.mark.asyncio
async def test_publish_uses_normalized_agent_key_title_record_commit_and_public_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent_id = uuid.uuid4()
    user_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    key = f"{agent_id}/workspace/index.html"
    storage = FakeStorage({key: "<TITLE>  Publish me  </TITLE>"})
    create = AsyncMock(return_value=SimpleNamespace(short_id="short-id"))
    patch_publish(
        monkeypatch,
        storage,
        agent=SimpleNamespace(tenant_id=tenant_id),
        create=create,
    )

    result = await publish._publish_page(
        agent_id, user_id, Path("compatibility-only"), {"path": "workspace/index.html"}
    )

    assert storage.keys == [key, key, key]
    assert result == (
        "Published successfully!\n\nPublic URL: https://pages.test/p/short-id\n"
        "Title: Publish me\n\nAnyone can access this page without logging in."
    )
    create.assert_awaited_once()
    obj_in = create.await_args.kwargs["obj_in"]
    assert (obj_in["agent_id"], obj_in["user_id"], obj_in["tenant_id"], obj_in["source_path"], obj_in["title"]) == (
        agent_id,
        user_id,
        tenant_id,
        "workspace/index.html",
        "Publish me",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    [
        "../victim-agent/page.html",
        "/victim-agent/page.html",
        "workspace//..//victim-agent/page.html",
        "./workspace/page.html",
        r"workspace\..\victim-agent\page.html",
        r"C:\victim-agent\page.html",
    ],
)
async def test_publish_rejects_unsafe_paths_before_storage_access(monkeypatch: pytest.MonkeyPatch, path: str) -> None:
    storage = FakeStorage({})
    backend_calls: list[str] = []

    def get_storage() -> FakeStorage:
        backend_calls.append("called")
        return storage

    monkeypatch.setattr(publish, "get_storage_backend", get_storage)

    assert (
        await publish._publish_page(uuid.uuid4(), uuid.uuid4(), Path("unused"), {"path": path})
        == f"Invalid path: {path}"
    )
    assert backend_calls == []
    assert storage.keys == []


@pytest.mark.asyncio
async def test_publish_missing_storage_and_tenant_failure_are_nonfatal_with_filename_title(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent_id = uuid.uuid4()
    create = AsyncMock(return_value=SimpleNamespace(short_id="short-id"))
    patch_publish(monkeypatch, FakeStorage({}), create=create)
    missing = await publish._publish_page(agent_id, uuid.uuid4(), Path("unused"), {"path": "workspace/missing.html"})
    assert missing == "File not found: workspace/missing.html"

    key = f"{agent_id}/workspace/fallback.html"
    storage = FakeStorage({key: "<body>No title</body>"})
    create2 = AsyncMock(return_value=SimpleNamespace(short_id="short-id"))
    get_agent = AsyncMock(side_effect=RuntimeError("tenant down"))
    monkeypatch.setattr(publish.agent_dao, "get", get_agent)
    monkeypatch.setattr(publish.published_page_dao, "create", create2)
    monkeypatch.setattr(publish, "get_storage_backend", lambda: storage)
    from app import config

    monkeypatch.setattr(config, "get_settings", lambda: SimpleNamespace(PUBLIC_BASE_URL=""))
    monkeypatch.setattr(secrets, "token_urlsafe", lambda _: "short-id-value")

    result = await publish._publish_page(agent_id, uuid.uuid4(), Path("unused"), {"path": "workspace/fallback.html"})

    assert "Public URL: /p/short-id" in result
    assert "PUBLIC_BASE_URL is not configured" in result
    obj_in = create2.await_args.kwargs["obj_in"]
    assert obj_in["tenant_id"] is None
    assert obj_in["title"] == "fallback"


@pytest.mark.asyncio
async def test_list_published_pages_formats_ordered_pages_and_empty_result(monkeypatch: pytest.MonkeyPatch) -> None:
    agent_id = uuid.uuid4()
    pages = [
        SimpleNamespace(short_id="new", title="Newest", source_path="workspace/new.html", view_count=2),
        SimpleNamespace(short_id="old", title=None, source_path="workspace/old.html", view_count=1),
    ]
    patch_publish(
        monkeypatch,
        FakeStorage({}),
        list_pages=AsyncMock(return_value=pages),
    )

    listed = await publish._list_published_pages(agent_id)
    assert listed == (
        "Published pages (2 total):\n\n- Newest\n  URL: https://pages.test/p/new\n"
        "  Source: workspace/new.html\n  Views: 2\n\n- Untitled\n"
        "  URL: https://pages.test/p/old\n  Source: workspace/old.html\n  Views: 1\n"
    )

    patch_publish(monkeypatch, FakeStorage({}), list_pages=AsyncMock(return_value=[]))
    assert await publish._list_published_pages(agent_id) == "No published pages yet."


@pytest.mark.asyncio
async def test_publish_facade_defers_and_preserves_workspace_and_arguments_identity(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from app.services import agent_tools

    publish_page = AsyncMock(return_value="published")
    list_pages = AsyncMock(return_value="listed")
    monkeypatch.setattr(publish, "_publish_page", publish_page)
    monkeypatch.setattr(publish, "_list_published_pages", list_pages)

    agent_id = uuid.uuid4()
    user_id = uuid.uuid4()
    arguments: ToolArguments = {"path": "workspace/index.html"}

    assert await agent_tools._publish_page(agent_id, user_id, tmp_path, arguments) == "published"
    assert await agent_tools._list_published_pages(agent_id) == "listed"
    publish_page.assert_awaited_once_with(agent_id, user_id, tmp_path, arguments)
    list_pages.assert_awaited_once_with(agent_id)
