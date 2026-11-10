import pytest
from tests.relationship_postgres_fixtures import RelationshipDatabase, relationship_database, serialize_contenders

from app.dao import agent_relationship_dao, identity_dao, org_member_dao, tenant_dao, user_dao
from app.db.session import connection_ctx

__all__ = ["relationship_database"]


@pytest.mark.parametrize(
    "change", ["user_inactive", "identity_inactive", "user_moved", "member_inactive", "member_moved"]
)
async def test_sync_rechecks_linked_member_eligibility_after_lock_wait(
    relationship_database: RelationshipDatabase, change: str
) -> None:
    data = relationship_database
    identity = await identity_dao.create_identity(email="linked@qa.test", email_verified=True)
    user = await user_dao.create(
        obj_in={"tenant_id": data.tenant_id, "identity_id": identity.id, "display_name": "Linked", "is_active": True}
    )
    member = data.members[0]
    await org_member_dao.update(db_obj=member, obj_in={"user_id": user.id})
    other = await tenant_dao.create(obj_in={"name": "Other tenant", "slug": identity.id.hex})

    async def revoke() -> None:
        statements = {
            "user_inactive": ("UPDATE users SET is_active = false WHERE id = %(id)s", user.id),
            "identity_inactive": ("UPDATE identities SET is_active = false WHERE id = %(id)s", identity.id),
            "user_moved": ("UPDATE users SET tenant_id = %(other)s WHERE id = %(id)s", user.id),
            "member_inactive": ("UPDATE org_members SET status = 'inactive' WHERE id = %(id)s", member.id),
            "member_moved": ("UPDATE org_members SET tenant_id = %(other)s WHERE id = %(id)s", member.id),
        }
        statement, row_id = statements[change]
        async with connection_ctx() as db:
            await db.execute(statement, {"id": row_id, "other": other.id})

    async def sync() -> None:
        stale = await user_dao.get_with_identity(user.id)
        assert stale is not None
        assert stale.is_active
        assert stale.identity is not None
        assert stale.identity.is_active
        assert stale.tenant_id == data.tenant_id
        response = await data.client.post("/api/okr/sync-relationships")
        assert response.status_code == 200, response.text

    await serialize_contenders(data, revoke, sync)

    rows = await agent_relationship_dao.list_for_agent(data.agent.id)
    assert len(rows) == 4
    assert {row.member_id for row in rows} == {item.id for item in data.members[1:]}


@pytest.mark.parametrize("change", ["inactive", "demoted"])
async def test_sync_reauthorizes_actor_after_lock_wait(
    relationship_database: RelationshipDatabase, change: str
) -> None:
    data = relationship_database
    original = await agent_relationship_dao.create(
        obj_in={"agent_id": data.agent.id, "member_id": data.members[0].id, "relation": "mentor"}
    )
    responses: list[int] = []

    async def revoke() -> None:
        statement = {
            "inactive": "UPDATE users SET is_active = false WHERE id = %(id)s",
            "demoted": "UPDATE users SET role = 'member' WHERE id = %(id)s",
        }[change]
        async with connection_ctx() as db:
            await db.execute(statement, {"id": data.actor.id})

    async def sync() -> None:
        response = await data.client.post("/api/okr/sync-relationships")
        responses.append(response.status_code)

    await serialize_contenders(data, revoke, sync)

    assert responses == [403]
    rows = await agent_relationship_dao.list_for_agent(data.agent.id)
    assert [(row.id, row.member_id, row.relation) for row in rows] == [(original.id, data.members[0].id, "mentor")]
