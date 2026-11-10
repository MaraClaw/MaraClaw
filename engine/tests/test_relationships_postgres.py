from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from tests.relationship_postgres_fixtures import RelationshipDatabase, relationship_database, serialize_contenders

from app.api import relationships
from app.core.domain.agents import AgentSharingMode
from app.dao import agent_relationship_dao, org_member_dao
from app.db.session import connection_ctx
from app.services import access_relationships, okr_agent_hook

__all__ = ["relationship_database"]


@pytest.mark.parametrize("same_member", [False, True])
async def test_racing_adds_preserve_members_and_update_existing_identity(
    relationship_database: RelationshipDatabase, same_member: bool
) -> None:
    data = relationship_database
    first_member = data.members[0]
    second_member = first_member if same_member else data.members[1]
    initial_id: list[UUID] = []

    async def first() -> None:
        await relationships.add_relationship(
            data.agent.id, relationships.RelationshipIn(member_id=str(first_member.id)), data.actor
        )
        row = await agent_relationship_dao.get_for_agent_and_member(data.agent.id, first_member.id)
        assert row is not None
        initial_id.append(row.id)

    async def second() -> None:
        await relationships.add_relationship(
            data.agent.id,
            relationships.RelationshipIn(member_id=str(second_member.id), description="Second"),
            data.actor,
        )

    await serialize_contenders(data, first, second)

    rows = await agent_relationship_dao.list_for_agent(data.agent.id)
    assert {row.member_id for row in rows} == {first_member.id, second_member.id}
    assert len(rows) == (1 if same_member else 2)
    original = next(row for row in rows if row.member_id == first_member.id)
    assert original.id == initial_id[0]
    assert original.created_by_user_id == data.actor.id
    if same_member:
        assert original.description == "Second"


@pytest.mark.parametrize("writer", ["hook", "backfill"])
@pytest.mark.parametrize("automatic_first", [True, False])
async def test_okr_writer_and_manual_add_share_serialization(
    relationship_database: RelationshipDatabase, writer: str, automatic_first: bool
) -> None:
    data = relationship_database
    member = data.members[0]
    if writer == "backfill":
        await org_member_dao.update(db_obj=member, obj_in={"user_id": data.actor.id})

    async def automatic() -> None:
        if writer == "hook":
            await okr_agent_hook.hook_new_org_member(member.id, data.tenant_id)
        else:
            await okr_agent_hook.sync_okr_agent_platform_members(data.tenant_id)

    async def manual() -> None:
        await relationships.add_relationship(
            data.agent.id, relationships.RelationshipIn(member_id=str(member.id), relation="mentor"), data.actor
        )

    await serialize_contenders(data, automatic if automatic_first else manual, manual if automatic_first else automatic)

    rows = await agent_relationship_dao.list_for_agent(data.agent.id)
    assert [(row.member_id, row.relation, row.created_by_user_id) for row in rows] == [
        (member.id, "mentor", None if automatic_first else data.actor.id)
    ]


async def test_access_reconciliation_rechecks_after_manual_add_and_keeps_one_member_row(
    relationship_database: RelationshipDatabase,
) -> None:
    data = relationship_database
    async with connection_ctx() as db:
        await db.execute("UPDATE agents SET access_mode = 'private' WHERE id = %(id)s", {"id": data.agent.id})
        await db.execute(
            "UPDATE org_members SET user_id = %(user)s WHERE id = %(id)s",
            {"id": data.members[0].id, "user": data.actor.id},
        )
    data.agent.access_mode = AgentSharingMode.PRIVATE

    async def first() -> None:
        await relationships.add_relationship(
            data.agent.id, relationships.RelationshipIn(member_id=str(data.members[0].id)), data.actor
        )

    async def second() -> None:
        assert await access_relationships.ensure_access_granted_platform_relationships(data.agent) is False

    await serialize_contenders(data, first, second)

    rows = await agent_relationship_dao.list_for_agent(data.agent.id)
    assert [row.member_id for row in rows] == [data.members[0].id]


async def test_access_reconciliation_and_nested_okr_hook_do_not_duplicate_new_platform_member(
    relationship_database: RelationshipDatabase,
) -> None:
    data = relationship_database
    async with connection_ctx() as db:
        await db.execute("UPDATE agents SET access_mode = 'private' WHERE id = %(id)s", {"id": data.agent.id})
    data.agent.access_mode = AgentSharingMode.PRIVATE

    changed = await access_relationships.ensure_access_granted_platform_relationships(data.agent)

    assert changed is True
    rows = await agent_relationship_dao.list_for_agent_with_members(data.agent.id)
    assert len(rows) == 1
    assert rows[0].member is not None
    assert rows[0].member.user_id == data.actor.id


@pytest.mark.parametrize("operation", ["replace", "delete"])
async def test_later_replace_or_delete_keeps_its_serial_removal_semantics(
    relationship_database: RelationshipDatabase, operation: str
) -> None:
    data = relationship_database
    member = data.members[0]
    created_id: list[UUID] = []

    async def first() -> None:
        await relationships.add_relationship(
            data.agent.id, relationships.RelationshipIn(member_id=str(member.id)), data.actor
        )
        row = await agent_relationship_dao.get_for_agent_and_member(data.agent.id, member.id)
        assert row is not None
        created_id.append(row.id)

    async def second() -> None:
        if operation == "replace":
            await relationships.save_relationships(
                data.agent.id,
                relationships.RelationshipBatchIn(
                    relationships=[relationships.RelationshipIn(member_id=str(data.members[1].id))]
                ),
                data.actor,
            )
        else:
            await relationships.delete_relationship(data.agent.id, created_id[0], data.actor)

    await serialize_contenders(data, first, second)

    rows = await agent_relationship_dao.list_for_agent(data.agent.id)
    assert [row.member_id for row in rows] == ([data.members[1].id] if operation == "replace" else [])


async def test_failed_replacement_rolls_back_deletion_and_inserts(
    relationship_database: RelationshipDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = relationship_database
    await data.client.post(data.path, json={"member_id": str(data.members[0].id)})
    before = await agent_relationship_dao.list_for_agent(data.agent.id)
    monkeypatch.setattr(
        relationships, "_regenerate_relationships_file", AsyncMock(side_effect=RuntimeError("QA failure"))
    )

    response = await data.client.put(data.path, json={"relationships": [{"member_id": str(data.members[1].id)}]})

    assert response.status_code == 500
    after = await agent_relationship_dao.list_for_agent(data.agent.id)
    assert [(row.id, row.member_id) for row in after] == [(row.id, row.member_id) for row in before]


async def test_add_rechecks_eligibility_after_waiting_for_tenant_lock(
    relationship_database: RelationshipDatabase,
) -> None:
    data = relationship_database
    member = data.members[0]
    responses: list[int] = []

    async def first() -> None:
        async with connection_ctx() as db:
            await db.execute("UPDATE org_members SET status = 'inactive' WHERE id = %(id)s", {"id": member.id})

    async def second() -> None:
        response = await data.client.post(data.path, json={"member_id": str(member.id)})
        responses.append(response.status_code)

    await serialize_contenders(data, first, second)

    assert responses == [400]
    assert await agent_relationship_dao.list_for_agent(data.agent.id) == []


async def test_http_pages_traverse_all_rows_and_add_preserves_unseen_contacts(
    relationship_database: RelationshipDatabase,
) -> None:
    data = relationship_database
    for member in data.members:
        response = await data.client.post(data.path, json={"member_id": str(member.id)})
        assert response.status_code == 200
    first = await data.client.get(data.path + "page", params={"limit": 2})
    page = first.json()
    rows = list(page["items"])
    sizes = [len(first.content)]
    while page["next_cursor"] is not None:
        response = await data.client.get(data.path + "page", params={"limit": 2, "cursor": page["next_cursor"]})
        assert response.status_code == 200
        page = response.json()
        assert len(page["items"]) <= 2
        rows.extend(page["items"])
        sizes.append(len(response.content))
    legacy = await data.client.get(data.path)
    assert legacy.status_code == 200
    assert rows == legacy.json()
    assert len(rows) == 5
    assert sizes[0] < len(legacy.content)
    for payload in ("!", "", uuid4().hex):
        invalid = await data.client.get(data.path + "page", params={"cursor": payload})
        assert invalid.status_code == 400
