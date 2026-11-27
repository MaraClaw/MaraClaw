from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException
from tests.bounded_read_fakes import RecordingStorage

from app.api import pages as pages_api
from app.config import get_settings
from app.services.agent_tool_exec.publish import PAGE_TOO_LARGE_PREFIX

PAGE_LIMIT = 4096


def _user(*, role: str = "member") -> Any:
    return SimpleNamespace(id=uuid.uuid4(), role=role, tenant_id=uuid.uuid4())


def _page(*, agent_id: uuid.UUID | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        short_id="abc12345",
        source_path="workspace/hello.html",
        title="Hello",
        view_count=3,
        created_at=None,
        agent_id=agent_id or uuid.uuid4(),
    )


@pytest.mark.asyncio
async def test_list_pages_returns_public_urls() -> None:
    page = _page()
    with (
        patch.object(pages_api, "check_agent_access", AsyncMock(return_value=(None, "use"))),
        patch.object(pages_api.published_page_dao, "list_for_agent", AsyncMock(return_value=[page])),
    ):
        rows = await pages_api.list_pages(page.agent_id, _user())

    assert rows[0]["url"] == "/p/abc12345"
    assert rows[0]["title"] == "Hello"


@pytest.mark.asyncio
async def test_publish_page_requires_manage() -> None:
    body = pages_api.PagePublish(agent_id=uuid.uuid4(), path="workspace/hello.html")
    with (
        patch.object(pages_api, "check_agent_access", AsyncMock(return_value=(None, "use"))),
        pytest.raises(HTTPException) as error,
    ):
        await pages_api.publish_page(body, _user())
    assert error.value.status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("refusal", "status"),
    [
        ("Only .html and .htm files can be published", 400),
        (f"{PAGE_TOO_LARGE_PREFIX}: workspace/huge.html (limit {PAGE_LIMIT} bytes)", 413),
    ],
)
async def test_publish_page_rejects_tool_errors(refusal: str, status: int) -> None:
    body = pages_api.PagePublish(agent_id=uuid.uuid4(), path="workspace/notes.txt")
    with (
        patch.object(pages_api, "check_agent_access", AsyncMock(return_value=(None, "manage"))),
        patch("app.services.agent_tool_exec.publish._publish_page", AsyncMock(return_value=refusal)),
        pytest.raises(HTTPException) as error,
    ):
        await pages_api.publish_page(body, _user())
    assert error.value.status_code == status


@pytest.mark.asyncio
async def test_unpublish_page_deletes_row() -> None:
    page = _page()
    delete = AsyncMock()
    with (
        patch.object(pages_api.published_page_dao, "get", AsyncMock(return_value=page)),
        patch.object(pages_api, "check_agent_access", AsyncMock(return_value=(None, "manage"))),
        patch.object(pages_api.published_page_dao, "delete", delete),
    ):
        result = await pages_api.unpublish_page(page.id, _user())
    assert result == {"deleted": True}
    delete.assert_awaited_once_with(record_id=page.id)


async def _render(page: SimpleNamespace, storage: RecordingStorage, views: AsyncMock) -> Any:
    with (
        patch.object(pages_api.published_page_dao, "get_by_short_id", AsyncMock(return_value=page)),
        patch.object(pages_api.published_page_dao, "increment_view_count", views),
        patch.object(pages_api, "get_storage_backend", lambda: storage),
    ):
        return await pages_api.render_page(page.short_id)


@pytest.mark.asyncio
async def test_render_page_serves_the_source_bytes_and_counts_one_view() -> None:
    page = _page()
    key = f"{page.agent_id}/{page.source_path}"
    html = "<title>Caf\u00e9</title><p>\u65e5\u672c</p>".encode()
    storage, views = RecordingStorage({key: html}), AsyncMock()

    response = await _render(page, storage, views)

    assert response.body == html
    assert response.headers["content-security-policy"] == "sandbox allow-scripts allow-forms allow-popups allow-modals"
    assert response.headers["x-content-type-options"] == "nosniff"
    views.assert_awaited_once_with(page.id)
    assert storage.reads == [(key, get_settings().PUBLISHED_PAGE_MAX_BYTES, None)]
    assert storage.whole_reads == []


@pytest.mark.asyncio
async def test_render_page_replaces_bytes_that_are_not_utf8() -> None:
    page = _page()
    key = f"{page.agent_id}/{page.source_path}"

    response = await _render(page, RecordingStorage({key: b"<p>ok\xff</p>"}), AsyncMock())

    assert response.body == "<p>ok\ufffd</p>".encode()


@pytest.mark.asyncio
async def test_render_page_serves_a_source_of_exactly_the_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "PUBLISHED_PAGE_MAX_BYTES", PAGE_LIMIT)
    page = _page()
    key = f"{page.agent_id}/{page.source_path}"
    html = b"<p>edge</p>".ljust(PAGE_LIMIT, b" ")
    storage, views = RecordingStorage({key: html}), AsyncMock()

    response = await _render(page, storage, views)

    assert response.body == html
    views.assert_awaited_once()
    assert storage.reads == [(key, PAGE_LIMIT, None)]


@pytest.mark.asyncio
async def test_render_page_answers_413_and_counts_no_view_for_a_source_over_the_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(get_settings(), "PUBLISHED_PAGE_MAX_BYTES", PAGE_LIMIT)
    page = _page()
    key = f"{page.agent_id}/{page.source_path}"
    storage, views = RecordingStorage({key: b"x" * (PAGE_LIMIT + 1)}), AsyncMock()

    with pytest.raises(HTTPException) as error:
        await _render(page, storage, views)

    assert error.value.status_code == 413
    views.assert_not_awaited()
    assert storage.reads == [(key, PAGE_LIMIT, None)]
    assert storage.whole_reads == []
