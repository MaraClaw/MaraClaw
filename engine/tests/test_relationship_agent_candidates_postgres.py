from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from tests.relationship_postgres_fixtures import (
    RelationshipDatabase,
    access_cache_unavailable,
    add_agent,
    add_tenant,
    add_user,
    grant_department,
    grant_user,
    relationship_database,
    statements,
)

from app.api import relationships
from app.core import access_cache
from app.core.domain.agents import PermissionAccessLevel
from app.core.domain.users import UserRole
from app.dao import user_dao
from app.db.session import connection_ctx
from app.records.agent import AgentRecord
from app.records.user import UserRecord

__all__ = ["access_cache_unavailable", "relationship_database", "statements"]


async def test_agent_candidates_keep_order_and_fixed_sql_cost_at_the_limit(
    relationship_database: RelationshipDatabase, access_cache_unavailable: None, statements: list[str]
) -> None:
    data = relationship_database
    _ = await user_dao.update(db_obj=data.actor, obj_in={"role": UserRole.MEMBER})
    data.actor.role = UserRole.MEMBER
    other = await add_user(data.tenant_id, "Candidate owner")
    path = data.path + "agent-candidates"
    counts: list[int] = []
    candidates: list[AgentRecord] = []
    start = datetime(2026, 1, 1, tzinfo=UTC)
    for size in (3, 51):
        for index in range(len(candidates), size):
            candidate = await add_agent(
                data.tenant_id, other.id, f"Picker {index}", fields={"created_at": start + timedelta(seconds=index)}
            )
            if index == 2:
                await grant_department(data.tenant_id, candidate.id, data.actor.id)
            else:
                await grant_user(candidate.id, data.actor.id, "manage")
            candidates.append(candidate)

        statements.clear()
        response = await data.client.get(path, params={"search": "pIcKeR"})
        counts.append(len(statements))

        assert response.status_code == 200
        assert [row["id"] for row in response.json()] == [
            str(agent.id) for agent in reversed(candidates[-50:]) if agent.id != candidates[2].id
        ]

    small, large = counts
    assert small == large, f"picker: {small} statements for 3 candidates, {large} for 50"


async def test_agent_candidates_ignore_stale_cached_manage_after_revocation(
    relationship_database: RelationshipDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = relationship_database
    _ = await user_dao.update(db_obj=data.actor, obj_in={"role": UserRole.MEMBER})
    data.actor.role = UserRole.MEMBER
    other = await add_user(data.tenant_id, "Candidate owner")
    target = await add_agent(data.tenant_id, other.id, "Direct")
    owned = await add_agent(data.tenant_id, data.actor.id, "Owned", mode="private")
    company = await add_agent(data.tenant_id, other.id, "Company", mode="company", level="manage")
    foreign_tenant = await add_tenant("Foreign")
    foreign_owner = await add_user(foreign_tenant, "Foreign owner")
    _ = await add_agent(foreign_tenant, foreign_owner.id, "Foreign", mode="company", level="manage")
    await grant_user(target.id, data.actor.id, "manage")

    snapshot = AsyncMock(return_value=access_cache.NO_ACL_SNAPSHOT)
    cached = AsyncMock(return_value=PermissionAccessLevel.MANAGE)
    fill = AsyncMock()
    monkeypatch.setattr(access_cache, "read_acl_snapshot", snapshot)
    monkeypatch.setattr(access_cache, "get_cached_level", cached)
    monkeypatch.setattr(access_cache, "set_cached_level", fill)

    for suffix, expected in (
        ("agent-candidates", {owned.id, company.id, target.id}),
        ("agents/candidates", {owned.id, company.id}),
    ):
        # This router-only app does not install the production request-memo middleware.
        access_cache.clear_request_memo()
        response = await data.client.get(data.path + suffix)
        assert response.status_code == 200
        assert {row["id"] for row in response.json()} == {str(agent_id) for agent_id in expected}
        # A downgrade leaves the target visible, so only a fresh manage check can remove it.
        if suffix == "agent-candidates":
            async with connection_ctx() as db:
                await db.execute(
                    "UPDATE agent_permissions SET access_level = 'use' WHERE agent_id = %(id)s",
                    {"id": target.id},
                )

    assert [args.args[1] for args in snapshot.await_args_list] == [data.agent.id, data.agent.id]
    assert [args.args[1] for args in cached.await_args_list] == [data.agent.id, data.agent.id]
    fill.assert_not_awaited()


@pytest.mark.parametrize("change", ["deactivate", "transfer"])
async def test_agent_candidates_recheck_persisted_membership_after_listing(
    relationship_database: RelationshipDatabase,
    access_cache_unavailable: None,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    data = relationship_database
    _ = await add_agent(data.tenant_id, data.actor.id, "Owned")
    foreign_tenant = await add_tenant("Transfer destination")
    list_visible = relationships.list_visible_agents

    async def change_membership_after_listing(
        user: UserRecord, *, tenant_id: UUID | None, exclude_agent_id: UUID, search: str | None, limit: int
    ) -> list[AgentRecord]:
        rows = await list_visible(
            user, tenant_id=tenant_id, exclude_agent_id=exclude_agent_id, search=search, limit=limit
        )
        assert len(rows) == 1
        async with connection_ctx() as db:
            if change == "deactivate":
                await db.execute("UPDATE users SET is_active = false WHERE id = %(id)s", {"id": user.id})
            else:
                await db.execute(
                    "UPDATE users SET tenant_id = %(tenant)s WHERE id = %(id)s",
                    {"id": user.id, "tenant": foreign_tenant},
                )
        return rows

    monkeypatch.setattr(relationships, "list_visible_agents", change_membership_after_listing)

    response = await data.client.get(data.path + "agent-candidates")

    assert response.status_code == 200
    assert response.json() == []


async def test_agent_candidates_deny_members_who_only_have_use_on_source(
    relationship_database: RelationshipDatabase, access_cache_unavailable: None
) -> None:
    data = relationship_database
    _ = await user_dao.update(db_obj=data.actor, obj_in={"role": UserRole.MEMBER})
    data.actor.role = UserRole.MEMBER
    other = await add_user(data.tenant_id, "Source owner")
    source = await add_agent(data.tenant_id, other.id, "Source", mode="company", level="use")

    response = await data.client.get(f"/api/agents/{source.id}/relationships/agent-candidates")

    assert response.status_code == 403
    assert response.json() == {"detail": "Only org admins or managers can modify relationships"}
