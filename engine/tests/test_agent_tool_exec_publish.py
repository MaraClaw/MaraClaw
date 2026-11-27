from __future__ import annotations

import secrets
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from tests.bounded_read_fakes import RecordingStorage

from app.config import Settings, get_settings
from app.services import platform_service as platform_module
from app.services.agent_tool_exec import publish
from app.services.agent_tool_exec.registry import ToolArguments

PAGE_LIMIT = 4096


def patch_publish(
    monkeypatch: pytest.MonkeyPatch,
    storage: RecordingStorage,
    *,
    public_base: str = "https://pages.test",
    agent: object | None = None,
    create: AsyncMock | None = None,
    list_pages: AsyncMock | None = None,
) -> AsyncMock:
    monkeypatch.setattr(publish, "get_storage_backend", lambda: storage)
    monkeypatch.setattr(platform_module, "get_settings", lambda: Settings(PUBLIC_BASE_URL=public_base))
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
    storage = RecordingStorage({key: b"<TITLE>  Publish me  </TITLE>"})
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

    assert storage.reads == [(key, publish.TITLE_HEAD_BYTES, 0)]
    assert storage.whole_reads == []
    assert result == (
        "Published successfully!\n\nPublic URL: https://pages.test/p/short-id\n"
        "Title: Publish me\n\nAnyone can access this page without logging in."
    )
    create.assert_awaited_once()
    assert create.await_args is not None
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
    storage = RecordingStorage({})
    backend_calls: list[str] = []

    def get_storage() -> RecordingStorage:
        backend_calls.append("called")
        return storage

    monkeypatch.setattr(publish, "get_storage_backend", get_storage)

    assert (
        await publish._publish_page(uuid.uuid4(), uuid.uuid4(), Path("unused"), {"path": path})
        == f"Invalid path: {path}"
    )
    assert backend_calls == []
    assert storage.reads == []


@pytest.mark.asyncio
async def test_publish_missing_storage_and_tenant_failure_are_nonfatal_with_filename_title(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent_id = uuid.uuid4()
    create = AsyncMock(return_value=SimpleNamespace(short_id="short-id"))
    patch_publish(monkeypatch, RecordingStorage({}), create=create)
    missing = await publish._publish_page(agent_id, uuid.uuid4(), Path("unused"), {"path": "workspace/missing.html"})
    assert missing == "File not found: workspace/missing.html"

    key = f"{agent_id}/workspace/fallback.html"
    storage = RecordingStorage({key: b"<body>No title</body>"})
    create2 = AsyncMock(return_value=SimpleNamespace(short_id="short-id"))
    get_agent = AsyncMock(side_effect=RuntimeError("tenant down"))
    monkeypatch.setattr(publish.agent_dao, "get", get_agent)
    monkeypatch.setattr(publish.published_page_dao, "create", create2)
    monkeypatch.setattr(publish, "get_storage_backend", lambda: storage)
    monkeypatch.setattr(platform_module, "get_settings", lambda: Settings(PUBLIC_BASE_URL=""))
    monkeypatch.setattr(secrets, "token_urlsafe", lambda _: "short-id-value")

    result = await publish._publish_page(agent_id, uuid.uuid4(), Path("unused"), {"path": "workspace/fallback.html"})

    assert "Public URL: /p/short-id" in result
    assert "PUBLIC_BASE_URL is not configured" in result
    assert create2.await_args is not None
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
        RecordingStorage({}),
        list_pages=AsyncMock(return_value=pages),
    )

    listed = await publish._list_published_pages(agent_id)
    assert listed == (
        "Published pages (2 total):\n\n- Newest\n  URL: https://pages.test/p/new\n"
        "  Source: workspace/new.html\n  Views: 2\n\n- Untitled\n"
        "  URL: https://pages.test/p/old\n  Source: workspace/old.html\n  Views: 1\n"
    )

    patch_publish(monkeypatch, RecordingStorage({}), list_pages=AsyncMock(return_value=[]))
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


@pytest.mark.asyncio
async def test_list_published_pages_uses_relative_links_when_base_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    pages = [SimpleNamespace(short_id="page", title="Page", source_path="page.html", view_count=0)]
    patch_publish(monkeypatch, RecordingStorage({}), public_base="", list_pages=AsyncMock(return_value=pages))
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://unowned.example")

    listed = await publish._list_published_pages(uuid.uuid4())

    assert "URL: /p/page" in listed
    assert "PUBLIC_BASE_URL" in listed
    assert "https://" not in listed


def published_title(create: AsyncMock) -> str:
    assert create.await_args is not None
    title = create.await_args.kwargs["obj_in"]["title"]
    assert isinstance(title, str)
    return title


@pytest.mark.asyncio
async def test_publish_takes_the_title_from_the_head_of_a_large_file(monkeypatch: pytest.MonkeyPatch) -> None:
    agent_id = uuid.uuid4()
    key = f"{agent_id}/workspace/big.html"
    page = "<title>Big \u20ac page</title>".encode().ljust(publish.TITLE_HEAD_BYTES * 8, b" ")
    storage = RecordingStorage({key: page})
    create = patch_publish(monkeypatch, storage)

    result = await publish._publish_page(agent_id, uuid.uuid4(), Path("unused"), {"path": "workspace/big.html"})

    assert result.startswith("Published successfully")
    assert published_title(create) == "Big \u20ac page"
    assert storage.reads == [(key, publish.TITLE_HEAD_BYTES, 0)]
    assert storage.whole_reads == []


@pytest.mark.asyncio
async def test_publish_ignores_a_title_that_ends_after_the_head(monkeypatch: pytest.MonkeyPatch) -> None:
    agent_id = uuid.uuid4()
    key = f"{agent_id}/workspace/late.html"
    storage = RecordingStorage({key: b"<title>" + b"t" * publish.TITLE_HEAD_BYTES + b"</title>"})
    create = patch_publish(monkeypatch, storage)

    result = await publish._publish_page(agent_id, uuid.uuid4(), Path("unused"), {"path": "workspace/late.html"})

    assert result.startswith("Published successfully")
    assert published_title(create) == "late"
    assert storage.reads == [(key, publish.TITLE_HEAD_BYTES, 0)]


@pytest.mark.asyncio
async def test_publish_keeps_the_title_when_a_character_is_cut_at_the_end_of_the_head(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent_id = uuid.uuid4()
    key = f"{agent_id}/workspace/cut.html"
    title = "<title>\u00dcn\u00ef</title>".encode()
    page = title.ljust(publish.TITLE_HEAD_BYTES - 1, b" ") + "\u20ac".encode() + b"<p>after</p>"
    create = patch_publish(monkeypatch, RecordingStorage({key: page}))

    await publish._publish_page(agent_id, uuid.uuid4(), Path("unused"), {"path": "workspace/cut.html"})

    assert published_title(create) == "\u00dcn\u00ef"


@pytest.mark.asyncio
async def test_publish_refuses_a_file_over_the_limit_before_reading_it_or_creating_a_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(get_settings(), "PUBLISHED_PAGE_MAX_BYTES", PAGE_LIMIT)
    agent_id = uuid.uuid4()
    key = f"{agent_id}/workspace/huge.html"
    storage = RecordingStorage({key: b"x" * (PAGE_LIMIT + 1)})
    create = patch_publish(monkeypatch, storage)

    result = await publish._publish_page(agent_id, uuid.uuid4(), Path("unused"), {"path": "workspace/huge.html"})

    assert result.startswith(publish.PAGE_TOO_LARGE_PREFIX)
    assert "workspace/huge.html" in result
    assert str(PAGE_LIMIT) in result
    create.assert_not_awaited()
    assert storage.reads == []


@pytest.mark.asyncio
async def test_publish_accepts_a_file_of_exactly_the_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "PUBLISHED_PAGE_MAX_BYTES", PAGE_LIMIT)
    agent_id = uuid.uuid4()
    key = f"{agent_id}/workspace/edge.html"
    storage = RecordingStorage({key: b"<title>Edge</title>".ljust(PAGE_LIMIT, b" ")})
    create = patch_publish(monkeypatch, storage)

    result = await publish._publish_page(agent_id, uuid.uuid4(), Path("unused"), {"path": "workspace/edge.html"})

    assert result.startswith("Published successfully")
    assert published_title(create) == "Edge"
