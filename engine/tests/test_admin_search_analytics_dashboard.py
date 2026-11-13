from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from fastapi.exceptions import ResponseValidationError
from psycopg import OperationalError
from tests.analytics_response_fixtures import dashboard_projection
from tests.membership_generation_fixtures import pin_membership_generation
from tests.test_admin_search_analytics_http_contracts import analytics_app, client

from app.api import admin_search_analytics as analytics
from app.core import security
from app.core.domain.users import UserRole
from app.dao.web_search_analytics_sql import SearchAnalyticsDashboardProjection
from app.records.identity import IdentityRecord
from app.records.user import UserRecord

__all__ = ["analytics_app", "client"]


@pytest.mark.parametrize("tenant_id", [None, UUID(int=10)])
async def test_dashboard_http_returns_one_resolved_mixed_scope_snapshot(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch, tenant_id: UUID | None
) -> None:
    calls: list[tuple[datetime, datetime, UUID | None]] = []
    payload = dashboard_projection()

    async def dashboard(
        *, start: datetime, end: datetime, tenant_id: UUID | None
    ) -> SearchAnalyticsDashboardProjection:
        calls.append((start, end, tenant_id))
        return payload

    monkeypatch.setattr(analytics.web_search_event_dao, "dashboard", dashboard)
    params = {"end": "2026-10-25T00:00:00"}
    if tenant_id is not None:
        params["tenant_id"] = str(tenant_id)

    response = await client.get("/api/admin/search-analytics/dashboard", params=params)

    end = datetime(2026, 10, 25, tzinfo=UTC)
    assert response.status_code == 200
    assert calls == [(end - timedelta(days=7), end, tenant_id)]
    assert response.json() == {
        **payload,
        "start": "2026-10-18T00:00:00Z",
        "end": "2026-10-25T00:00:00Z",
        "tenant_id": str(tenant_id) if tenant_id is not None else None,
        "system_summary": {**payload["system_summary"], "scope": "system"},
        "selected_summary": {
            **payload["selected_summary" if tenant_id is not None else "system_summary"],
            "scope": "org" if tenant_id is not None else "system",
        },
    }


@pytest.mark.parametrize(
    ("params", "status"),
    [
        ({"start": "2026-10-25", "end": "2026-10-25"}, 400),
        ({"start": "2026-10-26", "end": "2026-10-25"}, 400),
        ({"start": "2026-01-01", "end": "2026-10-25"}, 400),
        ({"tenant_id": "bad"}, 422),
        ({"end": "bad"}, 422),
    ],
)
async def test_dashboard_rejects_invalid_windows_before_persistence(
    client: httpx.AsyncClient, params: dict[str, str], status: int
) -> None:
    response = await client.get("/api/admin/search-analytics/dashboard", params=params)
    assert response.status_code == status


@pytest.mark.parametrize("role", [UserRole.MEMBER, UserRole.AGENT_ADMIN, UserRole.ORG_ADMIN])
async def test_dashboard_rejects_non_platform_admin(
    client: httpx.AsyncClient, analytics_app: FastAPI, role: UserRole
) -> None:
    analytics_app.dependency_overrides[security.get_current_user] = lambda: UserRecord(id=uuid4(), role=role)
    response = await client.get("/api/admin/search-analytics/dashboard")
    assert response.status_code == 403


async def test_dashboard_keeps_real_password_change_gate(
    client: httpx.AsyncClient, analytics_app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    analytics_app.dependency_overrides.clear()
    user = UserRecord(
        id=uuid4(),
        role=UserRole.PLATFORM_ADMIN,
        identity_id=uuid4(),
        identity=IdentityRecord(id=uuid4(), must_change_password=True),
    )

    async def principal(*args: object, **kwargs: object) -> UserRecord:
        return user

    async def generation(*args: object) -> int:
        return 0

    monkeypatch.setattr(security, "load_user_principal", principal)
    monkeypatch.setattr(security.identity_dao, "get_auth_generation", generation)
    pin_membership_generation(monkeypatch)
    token = await security.create_access_token(str(user.id), "platform_admin", identity_id=user.identity_id)
    response = await client.get("/api/admin/search-analytics/dashboard", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 403
    assert response.json()["detail"]["must_change_password"] is True


async def test_dashboard_aggregate_failure_remains_an_error(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def dashboard(**kwargs: object) -> SearchAnalyticsDashboardProjection:
        raise OperationalError("aggregation unavailable")

    monkeypatch.setattr(analytics.web_search_event_dao, "dashboard", dashboard)
    with pytest.raises(OperationalError, match="aggregation unavailable"):
        await client.get("/api/admin/search-analytics/dashboard")


async def test_dashboard_http_validates_nested_projection(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def dashboard(**kwargs: object) -> dict[str, object]:
        return {**dashboard_projection(), "orgs": [{"tenant_id": None, "event_count": 1, "quota_count": 0}]}

    monkeypatch.setattr(analytics.web_search_event_dao, "dashboard", dashboard)
    with pytest.raises(ResponseValidationError) as error:
        await client.get("/api/admin/search-analytics/dashboard")
    assert error.value.errors()[0]["loc"] == ("response", "orgs", 0, "name")
