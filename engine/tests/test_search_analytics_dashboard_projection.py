from datetime import UTC, datetime
from typing import assert_type
from uuid import UUID

import pytest
from pydantic import ValidationError
from tests.analytics_response_fixtures import dashboard_projection
from tests.test_analytics_projection_contracts import _RawConnection, raw

from app.dao.web_search_analytics_sql import SearchAnalyticsDashboardProjection
from app.dao.web_search_event_dao import web_search_event_dao

__all__ = ["raw"]

START = datetime(2026, 10, 20, tzinfo=UTC)
END = datetime(2026, 10, 21, tzinfo=UTC)


@pytest.mark.parametrize("tenant_id", [None, UUID(int=10)])
async def test_dashboard_uses_one_parameterized_aggregate_read(raw: _RawConnection, tenant_id: UUID | None) -> None:
    payload = dashboard_projection()
    raw.responses = [[{"dashboard": payload}]]

    result = await web_search_event_dao.dashboard(start=START, end=END, tenant_id=tenant_id)

    assert_type(result, SearchAnalyticsDashboardProjection)
    assert result == payload
    assert len(raw.executed) == 1
    assert raw.executed[0][1] == {"start": START, "end": END, "tenant_id": tenant_id}


@pytest.mark.parametrize("field", ["system_summary", "selected_summary", "timeseries", "orgs", "trending"])
async def test_dashboard_rejects_incomplete_database_aggregate(raw: _RawConnection, field: str) -> None:
    payload = {key: value for key, value in dashboard_projection().items() if key != field}
    raw.responses = [[{"dashboard": payload}]]

    with pytest.raises(ValidationError) as error:
        await web_search_event_dao.dashboard(start=START, end=END, tenant_id=None)

    assert error.value.errors()[0]["loc"] == (field,)
    assert len(raw.executed) == 1


async def test_dashboard_does_not_invent_zeroes_for_a_missing_database_row(raw: _RawConnection) -> None:
    raw.responses = [[]]

    with pytest.raises(ValidationError):
        await web_search_event_dao.dashboard(start=START, end=END, tenant_id=None)

    assert len(raw.executed) == 1
