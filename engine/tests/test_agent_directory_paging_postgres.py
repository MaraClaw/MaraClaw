from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from app.core import permissions
from app.schemas.agent_directory import AgentDirectoryPosition
from test_agent_access_explanation_postgres import AccessAPI

pytest_plugins = ("test_agent_access_explanation_postgres",)


async def test_directory_traverses_over_500_tied_and_null_rows(access_api: AccessAPI) -> None:
    api = access_api
    await api.db.execute("DELETE FROM agent_permissions")
    await api.db.execute("DELETE FROM agents")
    await api.db.execute(
        "INSERT INTO agents (id, name, role_description, creator_id, tenant_id, access_mode, "
        "company_access_level, max_triggers, min_poll_interval_min, webhook_rate_limit, heartbeat_interval_minutes, created_at) "
        "SELECT lpad(to_hex(n), 32, '0')::uuid, 'Agent ' || n, '', %(owner)s, %(tenant)s, 'company', 'use', 20, 5, 5, 240, "
        "CASE WHEN n <= 260 THEN TIMESTAMPTZ '2026-01-01' "
        "ELSE TIMESTAMPTZ '2026-02-01' END FROM generate_series(1, 510) n",
        {"owner": api.agent.creator_id, "tenant": api.viewer.tenant_id},
    )
    default = await api.client.get("/api/agents/")
    legacy = await api.client.get("/api/agents/?limit=500")
    assert (default.status_code, legacy.status_code) == (200, 200)
    assert (len(default.json()), len(legacy.json())) == (100, 500)
    assert (await api.client.get("/api/agents/?limit=501")).status_code == 422
    await api.db.execute("ALTER TABLE agents ALTER COLUMN created_at DROP NOT NULL")
    await api.db.execute("UPDATE agents SET created_at = NULL WHERE id <= lpad(to_hex(23), 32, '0')::uuid")

    ids: list[str] = []
    cursor = None
    while True:
        params = {"limit": "19"}
        if cursor is not None:
            params["cursor"] = cursor
        response = await api.client.get("/api/agents/page", params=params)
        assert response.status_code == 200, response.text
        page = response.json()
        assert len(page["items"]) <= 19
        ids.extend(row["id"] for row in page["items"])
        cursor = page["next_cursor"]
        assert page["has_more"] == (cursor is not None)
        if cursor is None:
            break
        assert len(ids) < 510

    assert ids == [str(UUID(int=n)) for n in range(510, 0, -1)]


@pytest.mark.parametrize("change", ["direct", "department", "membership", "inactive", "moved", "revoked"])
async def test_continuation_rechecks_current_visibility(access_api: AccessAPI, change: str) -> None:
    api = access_api
    after = AgentDirectoryPosition(datetime(2030, 1, 1, tzinfo=UTC), UUID(int=2**128 - 1))
    assert [agent.id for agent in await permissions.list_visible_agents(api.viewer, after=after, limit=5)] == [
        api.agent.id
    ]
    await api.db.execute("DELETE FROM agent_permissions WHERE scope_type = 'user'")
    await api.db.execute("DELETE FROM agent_permissions WHERE scope_type = 'department'")
    if change == "direct":
        await api.db.execute(
            "INSERT INTO agent_permissions (id, agent_id, scope_type, scope_id, access_level) "
            "VALUES (%(id)s, %(agent)s, 'user', %(user)s, 'use')",
            {"id": uuid4(), "agent": api.agent.id, "user": api.viewer.id},
        )
    elif change in ("department", "membership"):
        await api.db.execute(
            "INSERT INTO agent_permissions (id, agent_id, scope_type, scope_id, access_level) "
            "VALUES (%(id)s, %(agent)s, 'department', %(department)s, 'use')",
            {"id": uuid4(), "agent": api.agent.id, "department": api.department_ids[0]},
        )
        if change == "membership":
            await api.db.execute("DELETE FROM local_department_memberships")
    elif change == "inactive":
        await api.db.execute("UPDATE users SET is_active = FALSE WHERE id = %(id)s", {"id": api.viewer.id})
    elif change == "moved":
        await api.db.execute("DELETE FROM local_department_memberships WHERE user_id = %(id)s", {"id": api.viewer.id})
        await api.db.execute("UPDATE users SET tenant_id = NULL WHERE id = %(id)s", {"id": api.viewer.id})

    rows = await permissions.list_visible_agents(api.viewer, after=after, limit=5)

    assert [agent.id for agent in rows] == ([api.agent.id] if change in ("direct", "department") else [])
