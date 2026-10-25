import base64
import json
import re
import sqlite3
from collections.abc import Iterator, Mapping
from datetime import datetime, timedelta
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException

from app.api import schedules
from app.dao.activity_log_dao import agent_activity_log_dao
from app.db.connection import DbConnection
from test_schedule_dao import NOW, pool
from test_scheduler_api import _HTTP, api

__all__ = ["api", "pool"]


class _History:
    def __init__(self) -> None:
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.db.execute(
            "CREATE TABLE agent_activity_logs (id TEXT PRIMARY KEY, agent_id TEXT, action_type TEXT, "
            "summary TEXT, detail_json TEXT, related_id TEXT, created_at TEXT)"
        )

    def add(
        self,
        agent_id: UUID,
        schedule_id: UUID,
        *,
        created_at: datetime | None,
        log_id: UUID | None = None,
        action: str = "schedule_run",
        detail: Mapping[str, object] | None = None,
    ) -> UUID:
        row_id = log_id or uuid4()
        self.db.execute(
            "INSERT INTO agent_activity_logs VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                str(row_id),
                str(agent_id),
                action,
                "summary",
                json.dumps({"schedule_id": str(schedule_id), **(detail or {})}),
                None,
                created_at.isoformat() if created_at else None,
            ),
        )
        return row_id

    async def fetch(self, query: str, params: Mapping[str, object]) -> list[dict[str, object]]:
        sql = re.sub(r"%\((\w+)\)s", r":\1", query)
        values = {
            key: value.isoformat() if isinstance(value, datetime) else str(value) if isinstance(value, UUID) else value
            for key, value in params.items()
        }
        rows = self.db.execute(sql, values).fetchall()
        return [
            {
                **dict(row),
                "created_at": datetime.fromisoformat(row["created_at"]) if row["created_at"] else None,
                "detail_json": json.loads(row["detail_json"]),
            }
            for row in rows
        ]


@pytest.fixture
def history(monkeypatch: pytest.MonkeyPatch) -> Iterator[_History]:
    store = _History()

    async def fetch(_self: DbConnection, query: str, params: Mapping[str, object]) -> list[dict[str, object]]:
        return await store.fetch(query, params)

    monkeypatch.setattr(DbConnection, "fetchall", fetch)
    try:
        yield store
    finally:
        store.db.close()


@pytest.mark.parametrize("path", ["/history", "/history/page"])
async def test_history_scopes_before_limit_and_retains_deleted_schedule(
    api: _HTTP, history: _History, path: str
) -> None:
    api.get_schedule.return_value = None
    expected = [
        history.add(api.row.agent_id, api.row.id, created_at=NOW - timedelta(days=index)) for index in range(23)
    ]
    for _ in range(210):
        history.add(api.row.agent_id, uuid4(), created_at=NOW)
        history.add(uuid4(), api.row.id, created_at=NOW)
        history.add(api.row.agent_id, api.row.id, created_at=NOW, action="other")
    response = await api.client.get(api.url + path)
    assert response.status_code == 200
    items = response.json() if path == "/history" else response.json()["items"]
    assert [item["id"] for item in items] == [str(value) for value in expected[:20]]
    assert all(item["status"] == "unknown" for item in items)
    api.get_schedule.assert_not_awaited()


@pytest.mark.parametrize(
    ("detail", "expected"),
    [
        ({"status": "success"}, "success"),
        ({"status": "failure", "reply": "text"}, "failure"),
        ({"status": "cancelled"}, "cancelled"),
        ({"status": "unknown"}, "unknown"),
        ({"reply": "legacy successful-looking reply"}, "unknown"),
        ({}, "unknown"),
        ({"status": "failed"}, "failure"),
        ({"status": ["success"]}, "unknown"),
    ],
)
async def test_history_uses_only_explicit_terminal_status(
    api: _HTTP, history: _History, detail: dict[str, object], expected: str
) -> None:
    log_id = history.add(api.row.agent_id, api.row.id, created_at=None, detail=detail)
    response = await api.client.get(api.url + "/history/page")
    assert response.status_code == 200
    assert response.json() == {
        "items": [
            {
                "id": str(log_id),
                "created_at": None,
                "summary": "summary",
                "instruction": "",
                "reply": detail.get("reply", ""),
                "status": expected,
            }
        ],
        "next_cursor": None,
    }


async def test_history_keyset_preserves_ties_nulls_and_position_during_new_inserts(
    api: _HTTP, history: _History
) -> None:
    expected = [UUID(int=value) for value in [6, 5, 4, 3, 2, 1]]
    for log_id in expected:
        date = NOW if log_id.int >= 4 else NOW - timedelta(days=1) if log_id.int == 3 else None
        history.add(api.row.agent_id, api.row.id, created_at=date, log_id=log_id)
    cursor = None
    observed: list[str] = []
    for index in range(4):
        params = {"limit": "1" if index >= 2 else "2", **({"cursor": cursor} if cursor else {})}
        response = await api.client.get(api.url + "/history/page", params=params)
        assert response.status_code == 200
        page = response.json()
        observed.extend(item["id"] for item in page["items"])
        cursor = page["next_cursor"]
        if index == 0:
            history.add(api.row.agent_id, api.row.id, created_at=NOW + timedelta(days=1))
            history.add(api.row.agent_id, api.row.id, created_at=NOW, log_id=UUID(int=7))
            history.db.execute("DELETE FROM agent_activity_logs WHERE id = ?", (str(expected[1]),))
    assert observed == [str(value) for value in expected]
    assert cursor is None


@pytest.mark.parametrize("limit", [0, -1, 101, "invalid"])
async def test_history_rejects_invalid_page_size(api: _HTTP, limit: int | str) -> None:
    response = await api.client.get(api.url + "/history/page", params={"limit": limit})
    assert response.status_code == 422


async def test_history_accepts_maximum_page_size(api: _HTTP, history: _History) -> None:
    for _ in range(101):
        history.add(api.row.agent_id, api.row.id, created_at=None)
    response = await api.client.get(api.url + "/history/page", params={"limit": 100})
    assert response.status_code == 200
    assert len(response.json()["items"]) == 100
    assert response.json()["next_cursor"]


@pytest.mark.parametrize("cursor", ["", "!not-base64!", "e30", "null", "x" * 2049])
async def test_history_rejects_malformed_cursor(api: _HTTP, cursor: str) -> None:
    response = await api.client.get(api.url + "/history/page", params={"cursor": cursor})
    assert response.status_code in {400, 422}


@pytest.mark.parametrize("other_scope", ["agent", "schedule"])
async def test_history_cursor_cannot_cross_scope(api: _HTTP, history: _History, other_scope: str) -> None:
    for _ in range(2):
        history.add(api.row.agent_id, api.row.id, created_at=None)
    first = await api.client.get(api.url + "/history/page", params={"limit": 1})
    cursor = first.json()["next_cursor"]
    agent_id = uuid4() if other_scope == "agent" else api.row.agent_id
    schedule_id = uuid4() if other_scope == "schedule" else api.row.id
    response = await api.client.get(
        f"/agents/{agent_id}/schedules/{schedule_id}/history/page", params={"cursor": cursor}
    )
    assert response.status_code == 400


@pytest.mark.parametrize(
    "fault",
    [{"version": 2}, {"created_at": "2026-10-15T12:00:00"}, {"id": "invalid"}, {"extra": "invalid"}],
)
async def test_history_rejects_invalid_cursor_fields(api: _HTTP, fault: dict[str, object]) -> None:
    payload = {
        "version": 1,
        "agent_id": str(api.row.agent_id),
        "schedule_id": str(api.row.id),
        "created_at": NOW.isoformat(),
        "id": str(uuid4()),
        **fault,
    }
    cursor = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    response = await api.client.get(api.url + "/history/page", params={"cursor": cursor})
    assert response.status_code == 400


async def test_history_empty_page_has_no_cursor(api: _HTTP, history: _History) -> None:
    response = await api.client.get(api.url + "/history/page")
    assert response.status_code == 200
    assert response.json() == {"items": [], "next_cursor": None}


@pytest.mark.parametrize("path", ["/history", "/history/page"])
async def test_history_denies_access_before_query(api: _HTTP, monkeypatch: pytest.MonkeyPatch, path: str) -> None:
    monkeypatch.setattr(schedules, "check_agent_access", AsyncMock(side_effect=HTTPException(403, "Forbidden")))
    query = AsyncMock(side_effect=AssertionError("unauthorized history query"))
    monkeypatch.setattr(agent_activity_log_dao, "list_schedule_history", query)
    response = await api.client.get(api.url + path)
    assert response.status_code == 403
    query.assert_not_awaited()
