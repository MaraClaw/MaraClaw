from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from app.api import pages as pages_api


def _user(*, role: str = "member") -> SimpleNamespace:
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
async def test_publish_page_rejects_tool_errors() -> None:
    body = pages_api.PagePublish(agent_id=uuid.uuid4(), path="notes.txt")
    with (
        patch.object(pages_api, "check_agent_access", AsyncMock(return_value=(None, "manage"))),
        patch(
            "app.services.agent_tool_exec.publish._publish_page",
            AsyncMock(return_value="Only .html and .htm files can be published"),
        ),
        pytest.raises(HTTPException) as error,
    ):
        await pages_api.publish_page(body, _user())
    assert error.value.status_code == 400


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
    delete.assert_awaited_once_with(id=page.id)
