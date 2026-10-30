from datetime import UTC, datetime, timedelta
from uuid import UUID

import httpx
import pytest
from fastapi import Depends, FastAPI

from app.api import activity
from app.core.domain.activity import ActivityAction
from app.core.security import get_current_user, load_user_principal
from app.dao.activity_log_dao import agent_activity_log_dao
from app.db.session import bind_crud_connection, connection_ctx
from app.records.schedule import AgentScheduleRecord
from app.records.user import UserRecord
from test_schedule_occurrence_postgres import live_schedule

__all__ = ["live_schedule"]

NOW = datetime(2026, 10, 20, 12, tzinfo=UTC)


@pytest.fixture
async def manager_http(live_schedule: AgentScheduleRecord):
    app = FastAPI()
    app.include_router(activity.router, dependencies=[Depends(bind_crud_connection)])

    async def user() -> UserRecord:
        return await load_user_principal(live_schedule.created_by, fresh=True)

    app.dependency_overrides[get_current_user] = user
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://activity-qa") as client:
        yield client


async def _traverse(client: httpx.AsyncClient, agent_id: UUID, limit: int, **filters: str) -> list[dict[str, object]]:
    items: list[dict[str, object]] = []
    cursor: str | None = None
    while True:
        params = {"limit": str(limit), **filters, **({"cursor": cursor} if cursor is not None else {})}
        response = await client.get(f"/agents/{agent_id}/activity/page", params=params)
        assert response.status_code == 200
        body = response.json()
        items.extend(body["items"])
        next_cursor = body["next_cursor"]
        assert next_cursor is None or isinstance(next_cursor, str)
        cursor = next_cursor
        if cursor is None:
            return items


async def test_live_unknown_action_filter_returns_empty_page(
    live_schedule: AgentScheduleRecord, manager_http: httpx.AsyncClient
) -> None:
    response = await manager_http.get(
        f"/agents/{live_schedule.agent_id}/activity/page", params={"action_type": "future_action"}
    )
    assert response.status_code == 200
    assert response.json() == {"items": [], "next_cursor": None}


@pytest.mark.parametrize("limit", [1, 2, 3])
async def test_live_timeline_traverses_ties_and_nulls_once_in_order(
    live_schedule: AgentScheduleRecord, manager_http: httpx.AsyncClient, limit: int
) -> None:
    # Given timestamp ties, distinct timestamps, NULL timestamps and another agent's row.
    async with connection_ctx() as db:
        await db.execute("ALTER TABLE agent_activity_logs ALTER COLUMN created_at DROP NOT NULL")
    agent_id = live_schedule.agent_id
    layout = [(10, NOW), (9, NOW), (8, NOW), (7, NOW - timedelta(days=1)), (6, None), (5, None), (4, None)]
    for value, created_at in layout:
        await agent_activity_log_dao.create(
            obj_in={
                "id": UUID(int=value),
                "agent_id": agent_id,
                "action_type": "error" if value % 2 else "tool_call",
                "summary": f"row {value}",
                "detail_json": {"secret": "raw"},
                "created_at": created_at,
            }
        )
        if created_at is None:
            async with connection_ctx() as db:
                await db.execute(
                    "UPDATE agent_activity_logs SET created_at = NULL WHERE id = %(id)s", {"id": UUID(int=value)}
                )
    # When paging through the real route.
    everything = await _traverse(manager_http, agent_id, limit)
    errors = await _traverse(manager_http, agent_id, limit, action_type="error")
    # Then every row appears once in created_at DESC NULLS LAST, id DESC order, filtered exactly.
    assert [item["id"] for item in everything] == [str(UUID(int=value)) for value in [10, 9, 8, 7, 6, 5, 4]]
    assert [item["id"] for item in errors] == [str(UUID(int=value)) for value in [9, 7, 5]]
    assert everything[4]["created_at"] is None
    assert {tuple(item) for item in everything} == {("id", "action_type", "summary", "created_at")}
    assert ActivityAction(everything[0]["action_type"]) is ActivityAction.TOOL_CALL
