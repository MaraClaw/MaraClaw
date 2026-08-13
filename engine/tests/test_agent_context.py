import uuid
from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.asyncio
async def test_build_agent_context_does_not_inject_legacy_focus_file(monkeypatch):
    from app.services.agent_context import build_agent_context

    agent_id = uuid.uuid4()

    class EmptyResult:
        def scalar_one_or_none(self):
            return None

        def scalars(self):
            return self

        def all(self):
            return []

    class EmptySession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, _exc_type, _exc_value, _traceback):
            return None

        async def execute(self, _statement):
            return EmptyResult()

    monkeypatch.setattr("app.database.async_session", lambda: EmptySession(), raising=False)

    async def fake_read_file(key, _max_chars=3000):
        if key == f"{agent_id}/focus.md":
            return "# Focus\n\n- [ ] follow_up: Check the deployment"
        return ""

    with (
        patch("app.services.agent_context._read_file_safe", side_effect=fake_read_file),
        patch("app.services.agent_context._load_skills_index", new_callable=AsyncMock, return_value=""),
        patch("app.services.agent_context._load_relationships_from_db", new_callable=AsyncMock, return_value=""),
        patch("app.services.timezone_utils.get_agent_timezone", new_callable=AsyncMock, return_value="UTC"),
    ):
        _static, dynamic = await build_agent_context(agent_id, "TestAgent")

    assert "## Focus" not in dynamic
    assert "follow_up: Check the deployment" not in dynamic
