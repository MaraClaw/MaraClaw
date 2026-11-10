from typing import Literal, assert_never
from uuid import UUID

import pytest
from tests.relationship_postgres_fixtures import RelationshipDatabase, relationship_database, serialize_contenders

from app.api import relationships
from app.core.domain.agents import AgentSharingMode
from app.dao import agent_agent_relationship_dao, agent_dao, agent_relationship_dao, org_member_dao
from app.db.session import connection_ctx
from app.services import access_relationships, okr_agent_hook
from app.services.registration_service import registration_service

__all__ = ["relationship_database"]

type Writer = Literal["add", "replace", "delete", "hook", "backfill", "reconcile"]


@pytest.mark.parametrize("writer", ["add", "replace", "delete", "hook", "backfill", "reconcile"])
@pytest.mark.parametrize("sync_first", [True, False])
async def test_sync_serializes_with_manual_and_automatic_writers(
    relationship_database: RelationshipDatabase, writer: Writer, sync_first: bool
) -> None:
    data = relationship_database
    member = data.members[0]
    await org_member_dao.update(db_obj=member, obj_in={"user_id": data.actor.id})
    deleted_ids: list[UUID] = []
    match writer:
        case "reconcile":
            await agent_dao.update(db_obj=data.agent, obj_in={"access_mode": "private"})
            data.agent.access_mode = AgentSharingMode.PRIVATE
            provider = await registration_service.ensure_identity_provider("web", data.tenant_id, name="Platform")
            await org_member_dao.update(db_obj=member, obj_in={"provider_id": provider.id})
        case "delete":
            await relationships.add_relationship(
                data.agent.id, relationships.RelationshipIn(member_id=str(member.id)), data.actor
            )
            existing = await agent_relationship_dao.get_for_agent_and_member(data.agent.id, member.id)
            assert existing is not None
            deleted_ids.append(existing.id)
        case "add" | "replace" | "hook" | "backfill":
            pass
        case unreachable:
            assert_never(unreachable)

    async def sync() -> None:
        response = await data.client.post("/api/okr/sync-relationships")
        assert response.status_code == 200, response.text
        assert response.json() == {"status": "ok", "okr_agent_id": str(data.agent.id)}

    async def competing() -> None:
        match writer:
            case "add":
                await relationships.add_relationship(
                    data.agent.id, relationships.RelationshipIn(member_id=str(member.id), relation="mentor"), data.actor
                )
            case "replace":
                await relationships.save_relationships(
                    data.agent.id,
                    relationships.RelationshipBatchIn(
                        relationships=[relationships.RelationshipIn(member_id=str(member.id), relation="mentor")]
                    ),
                    data.actor,
                )
            case "delete":
                await relationships.delete_relationship(data.agent.id, deleted_ids[0], data.actor)
            case "hook":
                await okr_agent_hook.hook_new_org_member(member.id, data.tenant_id)
            case "backfill":
                await okr_agent_hook.sync_okr_agent_platform_members(data.tenant_id)
            case "reconcile":
                await access_relationships.ensure_access_granted_platform_relationships(data.agent)
            case unreachable:
                assert_never(unreachable)

    await serialize_contenders(data, sync if sync_first else competing, competing if sync_first else sync)

    rows = await agent_relationship_dao.list_for_agent(data.agent.id)
    expected = {item.id: "team_member" for item in data.members}
    if sync_first:
        match writer:
            case "replace":
                expected = {member.id: "mentor"}
            case "add":
                expected[member.id] = "mentor"
            case "delete" | "hook" | "backfill" | "reconcile":
                pass
            case unreachable:
                assert_never(unreachable)
    assert len(rows) == len(expected)
    assert {row.member_id: row.relation for row in rows} == expected
    assert {row.id for row in rows}.isdisjoint(deleted_ids)


async def test_sync_http_rebuilds_members_and_only_company_agents(
    relationship_database: RelationshipDatabase,
) -> None:
    data = relationship_database
    company = await agent_dao.create(
        obj_in={"name": "Company worker", "tenant_id": data.tenant_id, "creator_id": data.actor.id}
    )
    private = await agent_dao.create(
        obj_in={
            "name": "Private worker",
            "tenant_id": data.tenant_id,
            "creator_id": data.actor.id,
            "access_mode": "private",
        }
    )
    await agent_relationship_dao.create(
        obj_in={"agent_id": data.agent.id, "member_id": data.members[0].id, "relation": "mentor"}
    )
    await agent_agent_relationship_dao.create(obj_in={"agent_id": data.agent.id, "target_agent_id": private.id})

    response = await data.client.post("/api/okr/sync-relationships")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "okr_agent_id": str(data.agent.id)}
    humans = await agent_relationship_dao.list_for_agent(data.agent.id)
    agents = await agent_agent_relationship_dao.list_for_agent(data.agent.id)
    assert len(humans) == 5
    assert {(row.member_id, row.relation) for row in humans} == {(member.id, "team_member") for member in data.members}
    assert [(row.target_agent_id, row.relation) for row in agents] == [(company.id, "collaborator")]


async def test_sync_database_failure_rolls_back_both_relationship_sets(
    relationship_database: RelationshipDatabase,
) -> None:
    data = relationship_database
    original_human = await agent_relationship_dao.create(
        obj_in={"agent_id": data.agent.id, "member_id": data.members[0].id, "relation": "mentor"}
    )
    target = await agent_dao.create(
        obj_in={"name": "Company worker", "tenant_id": data.tenant_id, "creator_id": data.actor.id}
    )
    original_agent = await agent_agent_relationship_dao.create(
        obj_in={"agent_id": data.agent.id, "target_agent_id": target.id, "relation": "reviewer"}
    )
    async with connection_ctx() as db:
        await db.execute(
            "CREATE FUNCTION reject_relationship_insert() RETURNS trigger LANGUAGE plpgsql AS $$ "
            "BEGIN RAISE EXCEPTION 'Relationship QA insertion failure'; END $$"
        )
        await db.execute(
            "CREATE TRIGGER reject_relationship_insert BEFORE INSERT ON agent_agent_relationships "
            "FOR EACH ROW EXECUTE FUNCTION reject_relationship_insert()"
        )

    response = await data.client.post("/api/okr/sync-relationships")

    assert response.status_code == 500
    humans = await agent_relationship_dao.list_for_agent(data.agent.id)
    agents = await agent_agent_relationship_dao.list_for_agent(data.agent.id)
    assert [(row.id, row.member_id, row.relation) for row in humans] == [
        (original_human.id, data.members[0].id, "mentor")
    ]
    assert [(row.id, row.target_agent_id, row.relation) for row in agents] == [
        (original_agent.id, target.id, "reviewer")
    ]
