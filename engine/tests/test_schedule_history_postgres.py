from datetime import UTC, datetime
from uuid import UUID, uuid4

import httpx
import pytest

from app.core.json_types import JsonValue
from app.dao.activity_log_dao import agent_activity_log_dao
from app.db.session import connection_ctx
from app.records.schedule import AgentScheduleRecord
from app.schemas.schedule_history import ScheduleHistoryPage
from test_schedule_occurrence_postgres import (
    live_schedule,
    manual_http,
    manual_schedule,
)

__all__ = ["live_schedule", "manual_http", "manual_schedule"]


@pytest.mark.parametrize("outcome", ["all", "success", "failure", "cancelled", "unknown"])
async def test_live_history_normalizes_json_before_null_tie_keyset_limit(
    manual_schedule: AgentScheduleRecord, manual_http: httpx.AsyncClient, outcome: str
) -> None:
    # Given mixed historical JSON shapes, timestamp ties, NULL timestamps and unrelated rows.
    async with connection_ctx() as db:
        # Only this isolated test schema permits the nullable timestamps found in legacy history.
        await db.execute("ALTER TABLE agent_activity_logs ALTER COLUMN created_at DROP NOT NULL")
    details: list[JsonValue] = [
        {"status": "success"},
        {"status": "failed"},
        {"status": "failure"},
        {"status": "cancelled"},
        {"status": None},
        {"status": []},
        {"status": {}},
        {"status": 1},
        {"status": True},
        {},
        {"status": "other"},
    ]
    selected = {
        "all": list(range(11)),
        "success": [0],
        "failure": [1, 2],
        "cancelled": [3],
        "unknown": list(range(4, 11)),
    }[outcome]
    now = datetime.now(UTC)
    for index, detail in enumerate(details):
        assert isinstance(detail, dict)
        await agent_activity_log_dao.create(
            obj_in={
                "id": UUID(int=100 - index),
                "agent_id": manual_schedule.agent_id,
                "action_type": "schedule_run",
                "summary": "Historical",
                "detail_json": {"schedule_id": str(manual_schedule.id), **detail},
                "created_at": now if index < 6 else None,
            }
        )
    await agent_activity_log_dao.create(
        obj_in={
            "agent_id": manual_schedule.agent_id,
            "action_type": "schedule_run",
            "summary": "Other schedule",
            "detail_json": {"schedule_id": str(uuid4()), "status": "success"},
        }
    )
    # When traversing one filtered result per page through the actual PostgreSQL API.
    observed: list[str] = []
    cursor: str | None = None
    while True:
        params = {"outcome": outcome, "limit": "1", **({"cursor": cursor} if cursor is not None else {})}
        page = await manual_http.get(
            f"/agents/{manual_schedule.agent_id}/schedules/{manual_schedule.id}/history/page", params=params
        )
        assert page.status_code == 200
        result = ScheduleHistoryPage.model_validate(page.json())
        observed.extend(str(item.id) for item in result.items)
        cursor = result.next_cursor
        if cursor is None:
            break
    # Then SQL normalization and keysets return all and only the stable selected rows.
    assert observed == [str(UUID(int=100 - index)) for index in selected]
