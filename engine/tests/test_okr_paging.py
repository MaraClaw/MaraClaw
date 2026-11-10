from collections.abc import AsyncIterator, Sequence
from dataclasses import replace
from datetime import UTC, date, datetime
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.api import okr
from app.dao import org_member_dao
from app.dao.okr_dao import ObjectivePagePosition, ObjectivePageScope
from app.records.agent import AgentRecord
from app.records.okr import OKRKeyResultRecord, OKRObjectiveRecord, OKRSettingsRecord
from app.records.user import UserRecord
from app.schemas.okr import ObjectivePageOut
from app.services.okr_cursor import encode_objective_cursor

TENANT = UUID(int=1)
OWNER = UUID(int=2)
AGENT = UUID(int=3)
START = date(2026, 1, 1)
END = date(2026, 3, 31)
CLOCK = datetime(2026, 1, 2, tzinfo=UTC)
SCOPE = ObjectivePageScope(TENANT, START, END)
PERIOD = {"period_start": START.isoformat(), "period_end": END.isoformat()}


def objective(index: int) -> OKRObjectiveRecord:
    return OKRObjectiveRecord(
        id=UUID(int=100 + index),
        tenant_id=TENANT,
        title=f"Objective {index}",
        owner_type="user",
        owner_id=OWNER,
        period_start=START,
        period_end=END,
        created_at=CLOCK,
    )


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    app = FastAPI()
    app.include_router(okr.router)
    app.dependency_overrides[okr.get_current_user] = lambda: UserRecord(OWNER, tenant_id=TENANT)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://okr") as transport:
            yield transport
    finally:
        app.dependency_overrides.clear()


async def test_page_hydrates_only_retained_owners_and_complete_key_results(client, monkeypatch):
    rows = [
        replace(objective(1), owner_type="agent", owner_id=AGENT),
        replace(objective(2), owner_type="company", owner_id=None),
        objective(3),
        replace(objective(4), owner_id=UUID(int=999)),
    ]
    page_read = AsyncMock(return_value=rows)
    monkeypatch.setattr(okr.okr_objective_dao, "list_page", page_read)
    key_results = [
        OKRKeyResultRecord(id=UUID(int=1000 + i), objective_id=rows[2].id, title=f"KR {i}", created_at=CLOCK)
        for i in range(205)
    ]

    async def read_krs(ids: Sequence[UUID]) -> list[OKRKeyResultRecord]:
        return [kr for kr in key_results if kr.objective_id in ids]

    kr_read = AsyncMock(side_effect=read_krs)
    user_read = AsyncMock(return_value={OWNER: UserRecord(OWNER, tenant_id=TENANT, display_name="Ada")})
    agent_read = AsyncMock(return_value=[AgentRecord(AGENT, "Worker", OWNER, tenant_id=TENANT)])
    monkeypatch.setattr(okr.okr_key_result_dao, "list_for_objectives", kr_read)
    monkeypatch.setattr(okr.user_dao, "get_many_with_identity", user_read)
    monkeypatch.setattr(okr.agent_dao, "get_many", agent_read)
    monkeypatch.setattr(org_member_dao, "get_many", AsyncMock(return_value=[]))

    response = await client.get("/api/okr/objectives/page", params={**PERIOD, "limit": 3})

    assert response.status_code == 200
    page = ObjectivePageOut.model_validate_json(response.content)
    assert [(item.id, item.owner_name) for item in page.items] == [
        (str(rows[0].id), "Worker"),
        (str(rows[1].id), None),
        (str(rows[2].id), "Ada"),
    ]
    assert [kr.id for kr in page.items[2].key_results] == [str(UUID(int=1000 + i)) for i in range(205)]
    kr_read.assert_awaited_once_with([row.id for row in rows[:3]])
    user_read.assert_awaited_once_with([OWNER])
    agent_read.assert_awaited_once_with([AGENT])
    assert page.next_cursor is not None
    page_read.return_value = []
    await client.get("/api/okr/objectives/page", params={**PERIOD, "limit": 3, "cursor": page.next_cursor})
    assert page_read.await_args is not None
    assert page_read.await_args.kwargs["after"] == ObjectivePagePosition("user", CLOCK, rows[2].id)


@pytest.mark.parametrize("count", [0, 2])
async def test_empty_and_final_pages_have_no_continuation(client, monkeypatch, count):
    rows = [replace(objective(i), owner_type="company", owner_id=None) for i in range(count)]
    monkeypatch.setattr(okr.okr_objective_dao, "list_page", AsyncMock(return_value=rows))
    kr_read = AsyncMock(return_value=[])
    names = AsyncMock(return_value={})
    monkeypatch.setattr(okr.okr_key_result_dao, "list_for_objectives", kr_read)
    monkeypatch.setattr(okr, "owner_names", names)

    response = await client.get("/api/okr/objectives/page", params={**PERIOD, "limit": 2})

    assert response.status_code == 200
    page = ObjectivePageOut.model_validate_json(response.content)
    assert [item.id for item in page.items] == [str(row.id) for row in rows]
    assert page.next_cursor is None
    if count == 0:
        kr_read.assert_not_awaited()
        names.assert_not_awaited()


@pytest.mark.parametrize(
    "foreign_scope",
    [
        replace(SCOPE, tenant_id=UUID(int=9)),
        replace(SCOPE, period_start=date(2026, 1, 2)),
        replace(SCOPE, period_end=date(2026, 3, 30)),
        replace(SCOPE, owner_types=("agent",)),
    ],
    ids=["tenant", "period-start", "period-end", "owner-filter"],
)
async def test_cursor_rejects_foreign_scope_before_query(client, monkeypatch, foreign_scope):
    read = AsyncMock()
    monkeypatch.setattr(okr.okr_objective_dao, "list_page", read)
    token = encode_objective_cursor(foreign_scope, objective(1))

    response = await client.get("/api/okr/objectives/page", params={**PERIOD, "cursor": token})

    assert response.status_code == 400
    read.assert_not_awaited()


@pytest.mark.parametrize("raw", ["", "!not-base64!", "e30=", "\u2603", "tampered"])
async def test_malformed_and_tampered_cursors_return_400(client, monkeypatch, raw):
    read = AsyncMock()
    monkeypatch.setattr(okr.okr_objective_dao, "list_page", read)
    token = encode_objective_cursor(SCOPE, objective(1)) + "corrupt" if raw == "tampered" else raw

    response = await client.get("/api/okr/objectives/page", params={**PERIOD, "cursor": token})

    assert response.status_code == 400
    read.assert_not_awaited()


async def test_cursor_uses_effective_default_period(client, monkeypatch):
    settings = OKRSettingsRecord(TENANT)
    monkeypatch.setattr(okr, "_get_or_create_settings", AsyncMock(return_value=settings))
    monkeypatch.setattr(okr, "_compute_current_period", lambda *_: (date(2026, 4, 1), date(2026, 6, 30)))
    read = AsyncMock()
    monkeypatch.setattr(okr.okr_objective_dao, "list_page", read)

    response = await client.get(
        "/api/okr/objectives/page", params={"cursor": encode_objective_cursor(SCOPE, objective(1))}
    )

    assert response.status_code == 400
    read.assert_not_awaited()


async def test_cursor_accepts_reordered_duplicate_owner_filters(client, monkeypatch):
    scope = replace(SCOPE, owner_types=("agent", "user"))
    read = AsyncMock(return_value=[])
    monkeypatch.setattr(okr.okr_objective_dao, "list_page", read)
    token = encode_objective_cursor(scope, objective(1))

    response = await client.get(
        "/api/okr/objectives/page", params={**PERIOD, "cursor": token, "owner_types": ["user", "agent", "user"]}
    )

    assert response.status_code == 200
    read.assert_awaited_once_with(scope, limit=50, after=ObjectivePagePosition("user", CLOCK, objective(1).id))


@pytest.mark.parametrize(
    "query",
    [
        {"limit": "0"},
        {"limit": "201"},
        {"period_start": "bad"},
        {"period_end": "2025-01-01"},
        {"owner_types": "unknown"},
    ],
)
async def test_invalid_page_query_returns_422(client, monkeypatch, query):
    read = AsyncMock()
    monkeypatch.setattr(okr.okr_objective_dao, "list_page", read)

    response = await client.get("/api/okr/objectives/page", params={**PERIOD, **query})

    assert response.status_code == 422
    read.assert_not_awaited()
