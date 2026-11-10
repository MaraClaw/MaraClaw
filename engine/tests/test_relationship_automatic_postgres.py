from tests.relationship_postgres_fixtures import RelationshipDatabase, relationship_database, serialize_contenders

from app.api import relationships
from app.core.domain.agents import AgentSharingMode
from app.dao import agent_dao, agent_relationship_dao, identity_dao
from app.db.session import connection_ctx
from app.records.user import UserRecord
from app.services import access_relationships
from app.services.registration_service import registration_service

__all__ = ["relationship_database"]


async def test_registration_member_hook_preserves_a_concurrent_manual_relationship(
    relationship_database: RelationshipDatabase,
) -> None:
    data = relationship_database

    async def manual() -> None:
        await relationships.add_relationship(
            data.agent.id, relationships.RelationshipIn(member_id=str(data.members[0].id)), data.actor
        )

    async def register_member() -> None:
        member = await registration_service.ensure_web_org_member(data.actor)
        assert member is not None

    await serialize_contenders(data, manual, register_member)

    rows = await agent_relationship_dao.list_for_agent_with_members(data.agent.id)
    assert len(rows) == 2
    assert [row.member_id for row in rows if row.member is not None and row.member.user_id is None] == [
        data.members[0].id
    ]
    assert [row.member.user_id for row in rows if row.member is not None and row.member.user_id] == [data.actor.id]


async def test_registration_holds_tenant_before_new_user_foreign_keys_and_nested_hook(
    relationship_database: RelationshipDatabase,
) -> None:
    data = relationship_database
    identities = [
        await identity_dao.create_identity(email=f"person{index}@qa.test", email_verified=True) for index in range(2)
    ]
    users: list[UserRecord] = []

    async def first() -> None:
        users.append(await registration_service.create_user_with_identity(identities[0], tenant_id=data.tenant_id))

    async def second() -> None:
        users.append(await registration_service.create_user_with_identity(identities[1], tenant_id=data.tenant_id))

    await serialize_contenders(data, first, second)

    rows = await agent_relationship_dao.list_for_agent_with_members(data.agent.id)
    assert len(rows) == 2
    assert {row.member.user_id for row in rows if row.member is not None} == {user.id for user in users}


async def test_reconciliation_reloads_access_mode_after_waiting_for_tenant(
    relationship_database: RelationshipDatabase,
) -> None:
    data = relationship_database
    data.agent.access_mode = AgentSharingMode.PRIVATE
    await agent_dao.update(db_obj=data.agent, obj_in={"access_mode": "private"})
    changes: list[bool] = []

    async def revoke() -> None:
        async with connection_ctx() as db:
            await db.execute("UPDATE agents SET access_mode = 'company' WHERE id = %(id)s", {"id": data.agent.id})

    async def reconcile() -> None:
        changes.append(await access_relationships.ensure_access_granted_platform_relationships(data.agent))

    await serialize_contenders(data, revoke, reconcile)

    assert changes == [False]
    assert await agent_relationship_dao.list_for_agent(data.agent.id) == []
