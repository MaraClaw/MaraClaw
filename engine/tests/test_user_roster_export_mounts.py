import csv
from collections.abc import AsyncIterator
from io import StringIO
from unittest.mock import AsyncMock
from uuid import UUID

import anyio
import httpx
import pytest

import user_roster_export_fixtures as fixtures
from app import main
from app.api import users as users_api
from app.core.domain.users import UserRole
from app.core.security import create_access_token, get_current_user
from app.db.session import bind_crud_connection, get_connection
from app.records.tenant import TenantRecord
from app.records.user import UserRecord
from app.services.user_roster_export import export_user_roster
from user_roster_export_fixtures import TENANT, RosterDatabase, seed_matches

roster_single_db = fixtures.roster_single_db


async def test_mounted_user_page_enters_crud_binding(monkeypatch: pytest.MonkeyPatch) -> None:
    actor = UserRecord(id=UUID(int=1), tenant_id=TENANT, role=UserRole.ORG_ADMIN)
    entries: list[bool] = []

    async def crud_binding() -> AsyncIterator[None]:
        entries.append(True)
        yield

    monkeypatch.setitem(main.app.dependency_overrides, bind_crud_connection, crud_binding)
    monkeypatch.setitem(main.app.dependency_overrides, get_current_user, lambda: actor)
    monkeypatch.setattr(users_api.user_dao, "list_for_tenant_page", AsyncMock(return_value=[]))
    monkeypatch.setattr(users_api.agent_dao, "count_active_for_creators", AsyncMock(return_value={}))

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as client:
        response = await client.get("/api/users/page")

    assert response.status_code == 200
    assert entries == [True]


async def test_mounted_export_completes_without_entering_crud_binding(monkeypatch: pytest.MonkeyPatch) -> None:
    actor = UserRecord(id=UUID(int=1), tenant_id=TENANT, role=UserRole.ORG_ADMIN)

    async def reject_crud_binding() -> AsyncIterator[None]:
        raise AssertionError("export must not acquire the request-wide CRUD connection")
        yield

    monkeypatch.setitem(main.app.dependency_overrides, bind_crud_connection, reject_crud_binding)
    monkeypatch.setitem(main.app.dependency_overrides, get_current_user, lambda: actor)
    monkeypatch.setattr(
        users_api.tenant_dao, "get", AsyncMock(return_value=TenantRecord(id=TENANT, name="Own", slug="own"))
    )
    monkeypatch.setattr(users_api, "export_user_roster", AsyncMock(return_value="generated CSV"))

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as client:
        response = await client.get("/api/users/export")

    assert response.status_code == 200
    assert users_api.UserRosterExportOut.model_validate_json(response.content).csv == "generated CSV"


@pytest.mark.parametrize("requests", [1, 4], ids=["single", "concurrent"])
async def test_mounted_exports_complete_when_pool_has_one_connection(
    roster_single_db: RosterDatabase, monkeypatch: pytest.MonkeyPatch, requests: int
) -> None:
    await seed_matches(roster_single_db, 201)
    await roster_single_db.writer.execute(
        "INSERT INTO identities (id, email) VALUES (%(id)s, 'admin@example.com')",
        {"id": roster_single_db.actor.id},
    )
    await roster_single_db.writer.execute(
        "UPDATE users SET identity_id=%(id)s WHERE id=%(id)s", {"id": roster_single_db.actor.id}
    )
    token = await create_access_token(
        str(roster_single_db.actor.id), "org_admin", identity_id=roster_single_db.actor.id
    )
    entered: list[UUID] = []
    all_entered = anyio.Event()
    responses: dict[int, httpx.Response] = {}

    async def export(tenant_id: UUID, search: str, actor: UserRecord) -> str:
        assert get_connection() is None
        entered.append(actor.id)
        if len(entered) == requests:
            all_entered.set()
        await all_entered.wait()
        return await export_user_roster(tenant_id, search, actor)

    monkeypatch.setattr(users_api, "export_user_roster", export)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=main.app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}"},
    ) as client:

        async def request(index: int) -> None:
            search = "Match%_\\" if requests == 1 else f"Match%_\\ {index}"
            responses[index] = await client.get("/api/users/export", params={"search": search})

        with anyio.fail_after(10):
            async with anyio.create_task_group() as group:
                for index in range(requests):
                    group.start_soon(request, index)

    assert len(entered) == requests
    for index, response in responses.items():
        assert response.status_code == 200
        envelope = users_api.UserRosterExportOut.model_validate_json(response.content)
        rows = list(csv.reader(StringIO(envelope.csv, newline="")))[1:]
        expected = [10 + number for number in range(201) if requests == 1 or str(number).startswith(str(index))]
        assert [UUID(row[0]).int for row in rows] == expected
    assert await roster_single_db.writer.fetchval("SELECT COUNT(*) FROM admin_audit_logs") == requests
    stats = roster_single_db.pool.get_stats()
    assert stats["pool_size"] == stats["pool_available"] == 1
    assert stats["requests_waiting"] == 0
