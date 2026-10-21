import base64
import json
from datetime import timedelta
from uuid import UUID

import pytest

from app.schemas.schedule_history import ScheduleHistoryPage
from test_schedule_dao import NOW, pool as pool
from test_schedule_history import _History, history as history
from test_scheduler_api import _HTTP, api as api


@pytest.mark.parametrize("outcome", ["all", "success", "failure", "cancelled", "unknown"])
async def test_outcome_filters_before_limit_with_nulls_and_ties(api: _HTTP, history: _History, outcome: str) -> None:
    # Given interleaved normalized outcomes, including malformed JSON status values.
    statuses: list[object] = ["success", "failed", "failure", "cancelled", None, [], {}, 1, True, "other"]
    expected: list[str] = []
    for index, value in enumerate(statuses):
        log_id = UUID(int=100 - index)
        history.add(
            api.row.agent_id, api.row.id, id=log_id, created_at=NOW if index < 5 else None, detail={"status": value}
        )
        normalized = (
            "failure" if value in ("failed", "failure") else value if value in ("success", "cancelled") else "unknown"
        )
        if outcome == "all" or normalized == outcome:
            expected.append(str(log_id))
    # When traversing the SQL-filtered result a row at a time.
    observed: list[str] = []
    cursor: str | None = None
    while True:
        params = {"outcome": outcome, "limit": "1", **({"cursor": cursor} if cursor is not None else {})}
        response = await api.client.get(api.url + "/history/page", params=params)
        assert response.status_code == 200
        result = ScheduleHistoryPage.model_validate(response.json())
        observed.extend(str(item.id) for item in result.items)
        cursor = result.next_cursor
        if cursor is None:
            break
    # Then only the selected outcome is present, with stable NULL/tie ordering.
    assert observed == expected


@pytest.mark.parametrize("legacy", [False, True])
async def test_cursor_cannot_cross_outcomes(api: _HTTP, history: _History, legacy: bool) -> None:
    # Given either a legacy all-only position or a new outcome-bound cursor.
    for index in range(2):
        history.add(api.row.agent_id, api.row.id, created_at=NOW - timedelta(days=index), detail={"status": "success"})
    first = await api.client.get(api.url + "/history/page", params={"limit": 1})
    payload = json.loads(base64.urlsafe_b64decode(first.json()["next_cursor"] + "=="))
    if legacy:
        payload["version"] = 1
        del payload["outcome"]
    cursor = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    # When reusing the cursor for a different outcome.
    rejected = await api.client.get(api.url + "/history/page", params={"cursor": cursor, "outcome": "success"})
    accepted = await api.client.get(api.url + "/history/page", params={"cursor": cursor, "outcome": "all"})
    # Then the incompatible filter is rejected while all remains compatible.
    assert rejected.status_code == 400
    assert accepted.status_code == 200
    assert len(accepted.json()["items"]) == 1


@pytest.mark.parametrize("outcome", ["", "failed", "SUCCESS", "invalid"])
async def test_invalid_history_outcome_is_rejected(api: _HTTP, outcome: str) -> None:
    # Given a value outside the public normalized outcome vocabulary.
    # When parsing the query boundary.
    response = await api.client.get(api.url + "/history/page", params={"outcome": outcome})
    # Then it never reaches the DAO.
    assert response.status_code == 422
