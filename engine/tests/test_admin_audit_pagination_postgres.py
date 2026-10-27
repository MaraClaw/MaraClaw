from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient

from app.api import admin, users
from app.dao.admin_audit_dao import admin_audit_log_dao
from app.dao.identity_dao import identity_dao
from app.dao.user_dao import user_dao
from app.db.session import bind_crud_connection
from app.schemas.admin_audit import AdminAuditPageOut
from company_admission_fixtures import AdmissionScenario, admission_scenario

__all__ = ["admission_scenario"]

_PLATFORM = "/api/admin/audit-logs/page"
_ORG = "/api/users/admin-audit-logs/page"
_NOW = datetime(2026, 10, 24, 12, 0, 0, 123456, tzinfo=UTC)


@pytest.fixture
async def audit_client(admission_scenario: AdmissionScenario) -> AsyncIterator[AsyncClient]:
    app = FastAPI()
    for router in (admin.router, users.router):
        app.include_router(router, prefix="/api", dependencies=[Depends(bind_crud_connection)])
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://audit-postgres.test") as client:
        yield client


async def seed_audit(scenario: AdmissionScenario, positions: list[tuple[int, datetime]]) -> None:
    for number, created_at in positions:
        await admin_audit_log_dao.create(
            obj_in={
                "id": UUID(int=number),
                "actor_id": scenario.outsider.id,
                "actor_role": "platform_admin",
                "action": "fixture.audit",
                "target_type": "tenant",
                "target_id": scenario.target_id,
                "tenant_id": scenario.target_id,
                "created_at": created_at,
            }
        )


@pytest.mark.parametrize("path", [_PLATFORM, _ORG])
async def test_postgres_seek_walks_window_ties_once_and_exhausts_exact_full_page(
    admission_scenario: AdmissionScenario, audit_client: AsyncClient, path: str
):
    # Given six matching rows, including three UUID ties and adjacent microseconds.
    scenario = admission_scenario
    await seed_audit(
        scenario,
        [
            (10, _NOW - timedelta(microseconds=3)),
            (1, _NOW - timedelta(microseconds=2)),
            (9, _NOW - timedelta(microseconds=1)),
            (3, _NOW),
            (5, _NOW),
            (7, _NOW),
            (8, _NOW + timedelta(microseconds=1)),
            (11, _NOW + timedelta(microseconds=2)),
        ],
    )
    for number, different in enumerate(
        [{"tenant_id": scenario.source_id}, {"actor_id": scenario.genesis.id}, {"action": "other"}], start=20
    ):
        await admin_audit_log_dao.create(
            obj_in={
                "id": UUID(int=number),
                "actor_id": scenario.outsider.id,
                "actor_role": "platform_admin",
                "action": "fixture.audit",
                "target_type": "tenant",
                "tenant_id": scenario.target_id,
                "created_at": _NOW + timedelta(seconds=1),
                **different,
            }
        )
    params: dict[str, str | int] = {
        "tenant_id": str(scenario.target_id),
        "actor_id": str(scenario.outsider.id),
        "action": "fixture.audit",
        "start_at": "2026-10-24T14:00:00.123454+02:00",
        "end_at": "2026-10-24T07:00:00.123458-05:00",
        "limit": 2,
    }
    observed: list[UUID] = []
    pages: list[AdminAuditPageOut] = []
    # When the authenticated platform administrator follows every continuation.
    for _ in range(4):
        user = scenario.outsider if path == _PLATFORM else scenario.genesis
        response = await audit_client.get(path, params=params, headers=scenario.headers(user))
        assert response.status_code == 200, response.text
        page = AdminAuditPageOut.model_validate_json(response.content)
        pages.append(page)
        observed.extend(item.id for item in page.items)
        if page.next_cursor is None:
            break
        params["cursor"] = page.next_cursor
        params["start_at"] = (_NOW - timedelta(microseconds=2)).isoformat()
        params["end_at"] = (_NOW + timedelta(microseconds=2)).isoformat()
    # Then no ties, microsecond neighbors or filters are lost, duplicated or leaked.
    assert observed == [UUID(int=number) for number in (8, 7, 5, 3, 9, 1)]
    assert len(pages) == 3
    assert pages[0].items[-1].created_at == _NOW
    assert pages[-1].next_cursor is None


async def test_postgres_org_scope_ignores_client_tenant_and_actor(admission_scenario, audit_client):
    # Given a company event plus a newer foreign-company event.
    scenario = admission_scenario
    await seed_audit(scenario, [(1, _NOW)])
    await admin_audit_log_dao.create(
        obj_in={
            "id": UUID(int=2),
            "actor_role": "platform_admin",
            "action": "fixture.audit",
            "target_type": "tenant",
            "tenant_id": scenario.source_id,
            "created_at": _NOW + timedelta(seconds=1),
        }
    )
    # When an organization administrator supplies foreign tenant and actor parameters.
    response = await audit_client.get(
        _ORG,
        params={"tenant_id": str(scenario.source_id), "actor_id": str(scenario.genesis.id)},
        headers=scenario.headers(scenario.genesis),
    )
    # Then only the loaded principal's company is queried; those parameters grant nothing.
    assert response.status_code == 200, response.text
    page = AdminAuditPageOut.model_validate_json(response.content)
    assert [item.id for item in page.items] == [UUID(int=1)]
    assert page.next_cursor is None


@pytest.mark.parametrize("field", ["tenant_id", "actor_id", "action", "role", "principal_tenant", "start_at", "end_at"])
async def test_postgres_cursor_rejects_scope_changes(admission_scenario, audit_client, field):
    # Given an actual cursor from a two-row company trail.
    scenario = admission_scenario
    await seed_audit(scenario, [(1, _NOW), (2, _NOW)])
    org_page = field == "principal_tenant"
    path, user = (_ORG, scenario.genesis) if org_page else (_PLATFORM, scenario.outsider)
    params = {"tenant_id": str(scenario.target_id), "limit": "1"}
    first = await audit_client.get(path, params=params, headers=scenario.headers(user))
    assert first.status_code == 200, first.text
    cursor = AdminAuditPageOut.model_validate_json(first.content).next_cursor
    assert cursor is not None
    params["cursor"] = cursor
    match field:
        case "tenant_id":
            params[field] = str(scenario.source_id)
        case "actor_id":
            params[field] = str(scenario.genesis.id)
        case "action":
            params[field] = "other"
        case "role":
            path, user = _ORG, scenario.genesis
        case "principal_tenant":
            await user_dao.update(db_obj=user, obj_in={"tenant_id": scenario.source_id})
        case "start_at" | "end_at":
            params[field] = _NOW.isoformat()
    # When role, filters or the current principal's tenant no longer match.
    response = await audit_client.get(path, params=params, headers=scenario.headers(user))
    # Then neither a cursor nor a query parameter can supply authority.
    assert response.status_code == 400
    assert response.json()["detail"] == "Invalid cursor"


@pytest.mark.parametrize("path", [_PLATFORM, _ORG])
@pytest.mark.parametrize(
    ("state", "status"), [("retired", 401), ("inactive", 401), ("force_change", 403), ("wrong_role", 403)]
)
async def test_postgres_session_changes_gate_continuations_before_decode(
    admission_scenario, audit_client, path, state, status
):
    # Given a valid issued cursor and a real signed session.
    scenario = admission_scenario
    user = scenario.outsider if path == _PLATFORM else scenario.genesis
    await seed_audit(scenario, [(1, _NOW), (2, _NOW)])
    first = await audit_client.get(path, params={"limit": 1}, headers=scenario.headers(user))
    assert first.status_code == 200, first.text
    cursor = AdminAuditPageOut.model_validate_json(first.content).next_cursor
    assert cursor is not None
    assert user.identity is not None
    match state:
        case "retired":
            await identity_dao.bump_auth_generation(user.identity.id)
        case "inactive":
            await user_dao.update(db_obj=user, obj_in={"is_active": False})
        case "force_change":
            await identity_dao.update(db_obj=user.identity, obj_in={"must_change_password": True})
        case "wrong_role":
            await user_dao.update(db_obj=user, obj_in={"role": "member"})
    # When either the issued cursor or a malformed one is replayed after that change.
    responses = [
        await audit_client.get(path, params={"cursor": value}, headers=scenario.headers(user))
        for value in (cursor, "!")
    ]
    # Then current database-backed auth, not the cursor, determines access.
    assert [response.status_code for response in responses] == [status, status]


@pytest.mark.parametrize("path", [_PLATFORM, _ORG])
async def test_postgres_empty_filtered_page_is_exhausted(admission_scenario, audit_client, path):
    # Given no events matching an action.
    scenario = admission_scenario
    user = scenario.outsider if path == _PLATFORM else scenario.genesis
    # When either authorized administrator asks for that action.
    response = await audit_client.get(path, params={"action": "absent"}, headers=scenario.headers(user))
    # Then there is no synthetic continuation.
    assert response.status_code == 200, response.text
    assert response.json() == {"items": [], "next_cursor": None}


@pytest.mark.parametrize("path", [_PLATFORM, _ORG])
async def test_postgres_window_continuation_remains_nonsnapshot(admission_scenario, audit_client, path):
    # Given a window with two rows and an already issued continuation.
    scenario = admission_scenario
    user = scenario.outsider if path == _PLATFORM else scenario.genesis
    await seed_audit(scenario, [(2, _NOW), (4, _NOW)])
    params = {
        "start_at": (_NOW - timedelta(seconds=1)).isoformat(),
        "end_at": (_NOW + timedelta(seconds=1)).isoformat(),
        "limit": "1",
    }
    first = await audit_client.get(path, params=params, headers=scenario.headers(user))
    assert first.status_code == 200, first.text
    cursor = AdminAuditPageOut.model_validate_json(first.content).next_cursor
    assert cursor is not None
    await seed_audit(scenario, [(3, _NOW), (5, _NOW)])
    # When continuing after intervening inserts inside the same window.
    response = await audit_client.get(path, params={**params, "cursor": cursor}, headers=scenario.headers(user))
    # Then a late lower seek key appears, while a higher key requires a root refresh.
    assert response.status_code == 200, response.text
    page = AdminAuditPageOut.model_validate_json(response.content)
    assert [item.id for item in page.items] == [UUID(int=3)]
    assert page.next_cursor is not None
