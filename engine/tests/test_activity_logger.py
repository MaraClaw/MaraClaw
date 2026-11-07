from copy import deepcopy
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from app.core.domain.activity import ActivityAction
from app.core.json_types import JsonObject
from app.services import activity_logger


@pytest.mark.parametrize(
    ("summary", "detail", "expected_summary", "expected_detail"),
    [
        (
            "exec\x00 result",
            {"out\x00put": ["sqlite: s\x10U\x01w\x05\x00", {"nested": "a\x00b"}]},
            "exec\ufffd result",
            {"out\ufffdput": ["sqlite: s\x10U\x01w\x05\ufffd", {"nested": "a\ufffdb"}]},
        ),
        (
            "normal output",
            {"output": "caf\u00e9\n\t", "literal": r"\u0000", "values": [None, True, 3, 1.5, {}, []]},
            "normal output",
            {"output": "caf\u00e9\n\t", "literal": r"\u0000", "values": [None, True, 3, 1.5, {}, []]},
        ),
        ("\x00" + "x" * 500, None, "\ufffd" + "x" * 499, None),
        ("", {}, "", {}),
    ],
)
async def test_activity_payload_is_safe_without_mutating_input(
    monkeypatch: pytest.MonkeyPatch,
    summary: str,
    detail: JsonObject | None,
    expected_summary: str,
    expected_detail: JsonObject | None,
) -> None:
    create = AsyncMock()
    monkeypatch.setattr(activity_logger.agent_activity_log_dao, "create", create)
    agent_id, related_id = uuid4(), uuid4()
    original = deepcopy(detail)

    await activity_logger.log_activity(agent_id, ActivityAction.TOOL_CALL, summary, detail, related_id)

    create.assert_awaited_once_with(
        obj_in={
            "agent_id": agent_id,
            "action_type": ActivityAction.TOOL_CALL,
            "summary": expected_summary,
            "detail_json": expected_detail,
            "related_id": related_id,
        }
    )
    assert detail == original


async def test_activity_database_failure_remains_non_raising(monkeypatch: pytest.MonkeyPatch) -> None:
    create = AsyncMock(side_effect=RuntimeError("database unavailable"))
    error = Mock()
    monkeypatch.setattr(activity_logger.agent_activity_log_dao, "create", create)
    monkeypatch.setattr(activity_logger.logger, "error", error)

    await activity_logger.log_activity(uuid4(), ActivityAction.TOOL_CALL, "exec")

    error.assert_called_once()
