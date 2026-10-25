from collections.abc import AsyncIterator
from datetime import date
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import TypeAdapter, ValidationError

from app.api import admin as admin_api
from app.core import security
from app.core.domain.users import UserRole
from app.core.json_types import json_loads_value
from app.core.security import create_access_token
from app.records.identity import IdentityRecord
from app.records.user import UserRecord
from app.schemas.admin_metrics import (
    AdminChurnWarning,
    AdminEnhancedMetricsResponse,
    AdminLeaderboardsResponse,
    AdminTimeseriesPoint,
)


@pytest.fixture
def metric_user() -> UserRecord:
    identity = IdentityRecord(id=uuid4())
    return UserRecord(id=uuid4(), role=UserRole.PLATFORM_ADMIN, identity_id=identity.id, identity=identity)


@pytest.fixture
async def metric_client(monkeypatch: pytest.MonkeyPatch, metric_user: UserRecord) -> AsyncIterator[AsyncClient]:
    monkeypatch.setattr(admin_api.user_dao, "get_with_identity", AsyncMock(return_value=metric_user))
    monkeypatch.setattr(admin_api.user_dao, "get", AsyncMock(return_value=metric_user))
    monkeypatch.setattr(security.identity_dao, "get_auth_generation", AsyncMock(return_value=7))
    app = FastAPI()
    app.include_router(admin_api.router, prefix="/api")
    token = await create_access_token(str(metric_user.id), metric_user.role)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", headers={"Authorization": f"Bearer {token}"}
    ) as client:
        yield client


@pytest.mark.parametrize("company_day", [date(2026, 8, 1), "2026-08-01"])
@pytest.mark.parametrize("end_day", ["2026-08-03", "2026-07-31"])
async def test_timeseries_preserves_daily_gaps_totals_and_range(
    monkeypatch: pytest.MonkeyPatch, metric_client: AsyncClient, company_day: date | str, end_day: str
) -> None:
    first_day, last_day = date(2026, 8, 1), date(2026, 8, 3)
    monkeypatch.setattr(admin_api.tenant_dao, "counts_by_created_day", AsyncMock(return_value={company_day: 2}))
    monkeypatch.setattr(admin_api.user_dao, "counts_by_created_day", AsyncMock(return_value={company_day: 3}))
    monkeypatch.setattr(
        admin_api.agent_activity_log_dao, "tokens_by_day", AsyncMock(return_value={first_day: 6, last_day: 7})
    )
    monkeypatch.setattr(
        admin_api.agent_activity_log_dao, "cache_read_by_day", AsyncMock(return_value={first_day: 2, last_day: 1})
    )
    monkeypatch.setattr(
        admin_api.chat_session_dao, "counts_by_created_day", AsyncMock(return_value={first_day: 2, last_day: 3})
    )
    monkeypatch.setattr(admin_api.chat_session_dao, "dau_by_created_day", AsyncMock(return_value={first_day: 2}))
    monkeypatch.setattr(
        admin_api.chat_session_dao,
        "wau_mau_by_day",
        AsyncMock(return_value=({first_day: 4, date(2026, 8, 2): 5}, {first_day: 8, last_day: 9})),
    )
    monkeypatch.setattr(admin_api.tenant_dao, "count_created_before", AsyncMock(return_value=7))
    monkeypatch.setattr(admin_api.user_dao, "count_created_before", AsyncMock(return_value=11))
    monkeypatch.setattr(admin_api.agent_dao, "sum_tokens_created_before", AsyncMock(return_value=(1000, 100)))
    monkeypatch.setattr(admin_api.chat_session_dao, "count_created_before", AsyncMock(return_value=4))
    companies, users = (2, 3) if isinstance(company_day, date) else (0, 0)
    first = {
        "date": "2026-08-01",
        "new_companies": companies,
        "total_companies": 7 + companies,
        "new_users": users,
        "total_users": 11 + users,
        "new_tokens": 6,
        "total_tokens": 1006,
        "new_cache_read_tokens": 2,
        "total_cache_read_tokens": 102,
        "cache_hit_rate": 0.3333,
        "new_sessions": 2,
        "total_sessions": 6,
        "dau": 2,
        "wau": 4,
        "mau": 8,
    }
    gap = {
        **first,
        "date": "2026-08-02",
        "new_companies": 0,
        "new_users": 0,
        "new_tokens": 0,
        "new_cache_read_tokens": 0,
        "cache_hit_rate": 0.0,
        "new_sessions": 0,
        "dau": 0,
        "wau": 5,
        "mau": 0,
    }
    last = {
        **gap,
        "date": "2026-08-03",
        "new_tokens": 7,
        "total_tokens": 1013,
        "new_cache_read_tokens": 1,
        "total_cache_read_tokens": 103,
        "cache_hit_rate": 0.1429,
        "new_sessions": 3,
        "total_sessions": 9,
        "wau": 0,
        "mau": 9,
    }

    response = await metric_client.get(
        "/api/admin/metrics/timeseries",
        params={"start_date": "2026-08-01T00:00:00Z", "end_date": f"{end_day}T00:00:00Z"},
    )

    assert response.status_code == 200
    assert json_loads_value(response.text) == ([first, gap, last] if end_day == "2026-08-03" else [])


@pytest.mark.parametrize("populated", [True, False])
async def test_leaderboards_preserve_rows_normalization_and_zero_ratios(
    monkeypatch: pytest.MonkeyPatch, metric_client: AsyncClient, populated: bool
) -> None:
    monkeypatch.setattr(
        admin_api.agent_dao,
        "top_token_companies",
        AsyncMock(return_value=[{"name": "Acme", "total": 6, "cache_read": 2}, {"name": None}] if populated else []),
    )
    monkeypatch.setattr(
        admin_api.agent_dao,
        "top_token_agents",
        AsyncMock(
            return_value=[{"name": "Research", "company": "Acme", "tokens": 7, "cache_read_tokens": 1}, {}]
            if populated
            else []
        ),
    )
    expected = {
        "top_companies": [
            {"name": "Acme", "tokens": 6, "cache_read_tokens": 2, "cache_hit_rate": 0.3333},
            {"name": "", "tokens": 0, "cache_read_tokens": 0, "cache_hit_rate": 0.0},
        ]
        if populated
        else [],
        "top_agents": [
            {"name": "Research", "company": "Acme", "tokens": 7, "cache_read_tokens": 1, "cache_hit_rate": 0.1429},
            {"name": "", "company": "", "tokens": 0, "cache_read_tokens": 0, "cache_hit_rate": 0.0},
        ]
        if populated
        else [],
    }

    response = await metric_client.get("/api/admin/metrics/leaderboards")

    assert response.status_code == 200
    assert json_loads_value(response.text) == expected


@pytest.mark.parametrize("populated", [True, False])
async def test_enhanced_metrics_preserve_nested_rows_nulls_and_rounding(
    monkeypatch: pytest.MonkeyPatch, metric_client: AsyncClient, populated: bool
) -> None:
    channels = [{"channel": "web", "count": 4}, {"channel": "slack", "count": 2}] if populated else []
    categories = [{"category": "uncategorized", "count": 3}] if populated else []
    warnings = (
        [
            {"name": "Idle", "total_tokens": 10000001, "last_active": None, "days_inactive": None},
            {
                "name": "Dormant",
                "total_tokens": 12000000,
                "last_active": "2026-08-01T12:00:00+00:00",
                "days_inactive": 20,
            },
        ]
        if populated
        else []
    )
    monkeypatch.setattr(
        admin_api.agent_activity_log_dao, "sum_tokens_since", AsyncMock(return_value=101 if populated else 0)
    )
    monkeypatch.setattr(
        admin_api.chat_session_dao, "count_created_since", AsyncMock(return_value=6 if populated else 0)
    )
    monkeypatch.setattr(
        admin_api.chat_session_dao, "retention_7d", AsyncMock(return_value=(3, 2) if populated else (0, 0))
    )
    monkeypatch.setattr(admin_api.chat_session_dao, "channel_distribution_since", AsyncMock(return_value=channels))
    monkeypatch.setattr(admin_api.tool_dao, "top_enabled_categories", AsyncMock(return_value=categories))
    monkeypatch.setattr(admin_api.chat_session_dao, "churn_warnings", AsyncMock(return_value=warnings))

    response = await metric_client.get("/api/admin/metrics/enhanced")

    assert response.status_code == 200
    assert json_loads_value(response.text) == {
        "avg_tokens_per_session_30d": 17 if populated else 0,
        "retention_rate_7d": 66.7 if populated else 0.0,
        "last_week_active_companies": 3 if populated else 0,
        "retained_companies": 2 if populated else 0,
        "channel_distribution": channels,
        "tool_category_top10": categories,
        "churn_warnings": warnings,
    }


@pytest.mark.parametrize("endpoint", ["timeseries", "leaderboards", "enhanced"])
@pytest.mark.parametrize("role", ["member", "org_admin", "force_change", "anonymous"])
async def test_metric_routes_preserve_privilege_gates(
    metric_client: AsyncClient, metric_user: UserRecord, endpoint: str, role: str
) -> None:
    metric_user.role = UserRole(role)
    if role == "force_change":
        metric_user.role = UserRole.PLATFORM_ADMIN
        metric_user.identity = IdentityRecord(id=uuid4(), must_change_password=True)
    if role == "anonymous":
        metric_client.headers.pop("Authorization")

    response = await metric_client.get(
        f"/api/admin/metrics/{endpoint}", params={"start_date": "2026-08-01", "end_date": "2026-08-03"}
    )

    assert response.status_code == (401 if role == "anonymous" else 403)


async def test_timeseries_rejects_invalid_dates(metric_client: AsyncClient) -> None:
    response = await metric_client.get(
        "/api/admin/metrics/timeseries", params={"start_date": "invalid", "end_date": "2026-08-03"}
    )

    assert response.status_code == 422


@pytest.mark.parametrize(
    "adapter",
    [
        TypeAdapter[object](AdminTimeseriesPoint),
        TypeAdapter[object](AdminLeaderboardsResponse),
        TypeAdapter[object](AdminEnhancedMetricsResponse),
    ],
)
def test_metric_contracts_reject_missing_required_fields(adapter: TypeAdapter[object]) -> None:
    with pytest.raises(ValidationError):
        adapter.validate_python({})


@pytest.mark.parametrize("missing", ["last_active", "days_inactive"])
def test_churn_contract_requires_nullable_fields_to_be_present(missing: str) -> None:
    warning: dict[str, object] = {"name": "Idle", "total_tokens": 10000001, "last_active": None, "days_inactive": None}
    del warning[missing]

    with pytest.raises(ValidationError, match=missing):
        TypeAdapter(AdminChurnWarning).validate_python(warning)
