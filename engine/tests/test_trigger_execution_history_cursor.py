import base64
import json
from uuid import UUID

import pytest
from tests.trigger_history_fixtures import AGENT_ID, CREATED_AT, SECRET, TRIGGER_ID, history_raw as history_raw
from tests.trigger_history_http_fixtures import HistoryHTTP, history_http as history_http

from app.core.json_types import json_loads_object


def token(**changes: object) -> str:
    values: dict[str, object] = {
        "version": 1,
        "agent_id": str(AGENT_ID),
        "trigger_id": str(TRIGGER_ID),
        "created_at": CREATED_AT.isoformat(),
        "id": str(UUID(int=1)),
    }
    values.update(changes)
    return base64.urlsafe_b64encode(json.dumps(values).encode()).decode().rstrip("=")


@pytest.mark.parametrize(
    "cursor",
    [
        "",
        "!" + SECRET,
        "a",
        SECRET * 100,
        "eyJ2ZXJzaW9uIjoxfQ==",
        "////",
        "\u00e9",
        base64.urlsafe_b64encode(b"not json").decode().rstrip("="),
        base64.urlsafe_b64encode(b"\xff").decode().rstrip("="),
        base64.urlsafe_b64encode(b"[]").decode().rstrip("="),
        base64.urlsafe_b64encode(b"{}").decode().rstrip("="),
        token(version=2),
        token(version="1"),
        token(version=None),
        token(version=True),
        token(version=1.0),
        token(created_at="2026-10-23T01:02:03"),
        token(created_at="not-a-date"),
        token(created_at=None),
        token(agent_id=str(UUID(int=999))),
        token(trigger_id=str(UUID(int=999))),
        token(id="not-a-uuid"),
        token(agent_id="not-a-uuid"),
        token(trigger_id="not-a-uuid"),
        token(payload={"secret": SECRET}),
    ],
)
async def test_history_cursor_errors_are_generic_after_authorization(history_http: HistoryHTTP, cursor: str) -> None:
    # Given / When
    response = await history_http.client.get(history_http.url, params={"cursor": cursor})
    # Then
    assert response.status_code == 400
    assert response.json() == {"detail": "Invalid cursor"}
    assert history_http.scope.calls == ["access", "get"]
    assert not history_http.raw.queries
    assert SECRET not in response.text


async def test_history_requires_explicit_cursor_version(history_http: HistoryHTTP) -> None:
    # Given
    value = json_loads_object(base64.urlsafe_b64decode(token() + "=" * (-len(token()) % 4)))
    del value["version"]
    cursor = base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")
    # When
    response = await history_http.client.get(history_http.url, params={"cursor": cursor})
    # Then
    assert response.status_code == 400
    assert response.json() == {"detail": "Invalid cursor"}
    assert not history_http.raw.queries
