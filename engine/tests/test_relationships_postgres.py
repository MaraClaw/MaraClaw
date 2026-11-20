import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import anyio
import pytest
from fastapi import HTTPException
from tests.relationship_postgres_fixtures import (
    RelationshipDatabase,
    access_cache_unavailable,
    add_agent,
    add_member,
    add_tenant,
    add_user,
    grant_department,
    grant_user,
    relationship_database,
    serialize_contenders,
    statements,
)

from app.api import gateway, relationships
from app.core import access_cache
from app.core.acl_locks import lock_agent, lock_tenants
from app.core.domain.agents import AgentSharingMode, PermissionAccessLevel
from app.core.permissions import (
    RelationshipStatus,
    evaluate_agent_relationship_status,
    evaluate_agent_relationship_statuses,
    evaluate_human_relationship_status,
    evaluate_human_relationship_statuses,
)
from app.dao import (
    agent_agent_relationship_dao,
    agent_dao,
    agent_permission_dao,
    agent_relationship_dao,
    org_member_dao,
    user_dao,
)
from app.db.session import connection_ctx
from app.records.agent import AgentRecord
from app.records.agent_agent_relationship import AgentAgentRelationshipRecord
from app.records.agent_relationship import AgentRelationshipRecord
from app.records.user import UserRecord
from app.schemas.schemas import GatewaySendMessageRequest
from app.services import access_relationships, okr_agent_hook
from app.services.agent_context import _load_relationships_from_db

__all__ = ["access_cache_unavailable", "relationship_database", "statements"]


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


# --- Batched relationship access checks -------------------------------------------------------


def expected(status: str, reason: str | None = None) -> RelationshipStatus:
    return {"access_allowed": status == "active", "access_status": status, "access_status_reason": reason}


ACTIVE = expected("active")
CREATOR_LOST = expected("restricted", "relationship_creator_no_longer_manages_both_agents")
MANAGER_LOST = expected("restricted", "manager_no_longer_has_access_to_both_agents")
MISSING_AGENT = expected("missing_target", "source_or_target_not_found")
MISSING_CONTACT = expected("missing_target", "agent_or_member_not_found")
NO_PLATFORM_ACCESS = expected("restricted", "platform_user_no_agent_access")


@dataclass(frozen=True, slots=True)
class Cast:
    """The people who decide access in the parity worlds."""

    tenant: UUID
    other_tenant: UUID
    admin: UserRecord
    manager: UserRecord
    reader: UserRecord
    departmental: UserRecord
    outsider: UserRecord
    inactive: UserRecord
    foreign: UserRecord


async def build_cast(data: RelationshipDatabase) -> Cast:
    other_tenant = await add_tenant("Other company")
    return Cast(
        tenant=data.tenant_id,
        other_tenant=other_tenant,
        admin=data.actor,
        manager=await add_user(data.tenant_id, "Manager"),
        reader=await add_user(data.tenant_id, "Reader"),
        departmental=await add_user(data.tenant_id, "Departmental"),
        outsider=await add_user(data.tenant_id, "Outsider"),
        inactive=await add_user(data.tenant_id, "Inactive", active=False),
        foreign=await add_user(other_tenant, "Foreign administrator", role="org_admin"),
    )


async def shared_agent(
    cast: Cast,
    name: str,
    *,
    creator: UserRecord | None = None,
    mode: str = "custom",
    manage: Sequence[UserRecord] = (),
    use: Sequence[UserRecord] = (),
    departmental: Sequence[UserRecord] = (),
    **fields: object,
) -> AgentRecord:
    """An agent created by the administrator (unless told otherwise) with direct and department grants."""
    agent = await add_agent(cast.tenant, (creator or cast.admin).id, name, mode=mode, fields=fields)
    for user in manage:
        await grant_user(agent.id, user.id, "manage")
    for user in use:
        await grant_user(agent.id, user.id, "use")
    for user in departmental:
        await grant_department(cast.tenant, agent.id, user.id)
    return agent


async def test_batched_agent_statuses_match_the_per_row_evaluator_on_every_precedence_branch(
    relationship_database: RelationshipDatabase, access_cache_unavailable: None
) -> None:
    data = relationship_database
    cast = await build_cast(data)
    admin, manager, outsider = cast.admin, cast.manager, cast.outsider
    source = await shared_agent(
        cast, "Source", manage=[manager, cast.inactive], use=[cast.reader], departmental=[cast.departmental]
    )
    company_source = await add_agent(cast.tenant, admin.id, "Company source", mode="company", level="manage")
    company_target = await add_agent(cast.tenant, admin.id, "Company target", mode="company")
    private_target = await shared_agent(cast, "Private target", creator=manager, mode="private")
    past = datetime.now(UTC) - timedelta(days=1)

    wanted: dict[str, RelationshipStatus] = {}
    current_users: dict[str, UserRecord | None] = {}

    async def case(
        name: str,
        target: AgentRecord,
        want: RelationshipStatus,
        *,
        origin: AgentRecord = source,
        created_by: UserRecord | None = None,
        current: UserRecord | None = None,
    ) -> None:
        _ = await agent_agent_relationship_dao.create(
            obj_in={
                "agent_id": origin.id,
                "target_agent_id": target.id,
                "relation": "peer",
                "description": name,
                "created_by_user_id": created_by.id if created_by else None,
            }
        )
        wanted[name], current_users[name] = want, current

    # Missing agent, then different tenant, then target availability come before any user is asked.
    await case(
        "stopped",
        await shared_agent(cast, "t1", status="stopped"),
        expected("restricted", "target_status_stopped"),
        created_by=manager,
    )
    await case(
        "errored",
        await shared_agent(cast, "t2", status="error"),
        expected("restricted", "target_status_error"),
        created_by=manager,
    )
    await case(
        "flagged expired",
        await shared_agent(cast, "t3", is_expired=True),
        expected("restricted", "target_expired"),
        created_by=manager,
    )
    await case(
        "past expiry",
        await shared_agent(cast, "t4", expires_at=past),
        expected("restricted", "target_expired"),
        created_by=manager,
    )
    foreign_target = await add_agent(cast.other_tenant, cast.foreign.id, "Foreign target")
    await case("other company", foreign_target, expected("restricted", "different_tenant"), created_by=manager)

    # A relationship creator decides alone and never falls through to company mode.
    await case("creator manages both", await shared_agent(cast, "t5", manage=[manager]), ACTIVE, created_by=manager)
    await case(
        "creator only uses the target", await shared_agent(cast, "t6", use=[manager]), CREATOR_LOST, created_by=manager
    )
    await case(
        "creator never had access", await shared_agent(cast, "t7", manage=[outsider]), CREATOR_LOST, created_by=outsider
    )
    await case(
        "creator deactivated",
        await shared_agent(cast, "t8", manage=[cast.inactive]),
        CREATOR_LOST,
        created_by=cast.inactive,
    )
    await case("creator from another company", await shared_agent(cast, "t9"), CREATOR_LOST, created_by=cast.foreign)
    await case(
        "creator reaches the target by department",
        await shared_agent(cast, "t10", departmental=[cast.departmental]),
        CREATOR_LOST,
        created_by=cast.departmental,
    )
    await case("creator owns both", await shared_agent(cast, "t11"), ACTIVE, created_by=admin)
    await case(
        "creator is not rescued by a company target", company_target, CREATOR_LOST, created_by=outsider, current=admin
    )

    # Without a creator: a company target, then the current user and the source agent's creator.
    await case("company target", company_target, ACTIVE)
    await case(
        "company managed pair",
        await add_agent(cast.tenant, admin.id, "t12", mode="company", level="manage"),
        ACTIVE,
        origin=company_source,
        created_by=outsider,
    )
    await case("current user manages both", await shared_agent(cast, "t13"), ACTIVE, current=admin)
    await case("source creator fallback", await shared_agent(cast, "t14"), ACTIVE, current=outsider)
    await case("nobody manages the private target", private_target, MANAGER_LOST, current=outsider)
    await case("current user is the source creator", private_target, MANAGER_LOST, current=admin)
    await case("no current user and a private target", private_target, MANAGER_LOST)
    await case("no current user and a shared target", await shared_agent(cast, "t15"), ACTIVE)

    records = [
        *await agent_agent_relationship_dao.list_for_agent_with_targets(source.id),
        *await agent_agent_relationship_dao.list_for_agent_with_targets(company_source.id),
        AgentAgentRelationshipRecord(uuid4(), source.id, uuid4(), description="target row missing"),
        AgentAgentRelationshipRecord(uuid4(), uuid4(), source.id, description="source row missing"),
    ]
    wanted["target row missing"] = wanted["source row missing"] = MISSING_AGENT
    current_users["target row missing"] = current_users["source row missing"] = None

    by_current: dict[UUID | None, list[AgentAgentRelationshipRecord]] = {}
    for rel in records:
        user = current_users[rel.description]
        by_current.setdefault(user.id if user else None, []).append(rel)
    per_row: dict[str, RelationshipStatus] = {}
    batched: dict[str, RelationshipStatus] = {}
    for current_id, rels in by_current.items():
        for rel in rels:
            per_row[rel.description] = await evaluate_agent_relationship_status(rel, current_user_id=current_id)
        statuses = await evaluate_agent_relationship_statuses(rels, current_user_id=current_id)
        batched.update({rel.description: status for rel, status in zip(rels, statuses, strict=True)})

    assert per_row == wanted
    assert batched == per_row


async def test_batched_human_statuses_match_the_per_row_evaluator_on_every_precedence_branch(
    relationship_database: RelationshipDatabase, access_cache_unavailable: None
) -> None:
    data = relationship_database
    cast = await build_cast(data)
    admin, manager = cast.admin, cast.manager
    custom = await shared_agent(
        cast, "Custom", manage=[manager, cast.inactive], use=[cast.reader], departmental=[cast.departmental]
    )
    company = await add_agent(cast.tenant, admin.id, "Company", mode="company")
    without_company_level = await add_agent(cast.tenant, admin.id, "No company level", mode="company", level="")
    private = await shared_agent(cast, "Private", creator=manager, mode="private")

    wanted: dict[str, RelationshipStatus] = {}

    async def case(
        name: str,
        origin: AgentRecord,
        want: RelationshipStatus,
        *,
        user: UserRecord | None = None,
        user_id: UUID | None = None,
        member_tenant: UUID | None = cast.tenant,
        status: str = "active",
    ) -> None:
        member = await add_member(member_tenant, name, user_id=user.id if user else user_id, status=status)
        _ = await agent_relationship_dao.create(
            obj_in={
                "agent_id": origin.id,
                "member_id": member.id,
                "relation": "collaborator",
                "description": name,
                "created_by_user_id": admin.id,
            }
        )
        wanted[name] = want

    # The member decides first, then the linked platform user's access to the agent.
    await case(
        "inactive contact", custom, expected("restricted", "member_inactive"), user=cast.reader, status="inactive"
    )
    await case(
        "contact of another company",
        custom,
        expected("restricted", "different_tenant"),
        user=cast.reader,
        member_tenant=cast.other_tenant,
    )
    await case("contact without a company", custom, ACTIVE, user=cast.reader, member_tenant=None)
    await case("contact without a platform user", custom, ACTIVE)
    await case("company agent", company, ACTIVE, user=cast.outsider)
    await case("company agent without a company level", without_company_level, ACTIVE, user=cast.outsider)
    await case("direct grant", custom, ACTIVE, user=cast.reader)
    await case("department grant", custom, ACTIVE, user=cast.departmental)
    await case("no grant", custom, NO_PLATFORM_ACCESS, user=cast.outsider)
    await case("deactivated user", custom, NO_PLATFORM_ACCESS, user=cast.inactive)
    await case("user of another company", custom, NO_PLATFORM_ACCESS, user=cast.foreign)
    await case("unknown user", custom, NO_PLATFORM_ACCESS, user_id=uuid4())
    await case("administrator on a shared agent", custom, ACTIVE, user=admin)
    await case("administrator on a private agent", private, NO_PLATFORM_ACCESS, user=admin)
    await case("owner of a private agent", private, ACTIVE, user=manager)

    loose = await add_member(cast.tenant, "loose", user_id=cast.reader.id)
    records = [
        *[
            rel
            for origin in (custom, company, without_company_level, private)
            for rel in await agent_relationship_dao.list_for_agent_with_members(origin.id)
        ],
        AgentRelationshipRecord(uuid4(), uuid4(), loose.id, description="agent row missing"),
        AgentRelationshipRecord(uuid4(), custom.id, uuid4(), description="contact row missing"),
        AgentRelationshipRecord(uuid4(), custom.id, loose.id, description="contact loaded by id"),
    ]
    wanted["agent row missing"] = wanted["contact row missing"] = MISSING_CONTACT
    wanted["contact loaded by id"] = ACTIVE

    per_row = {rel.description: await evaluate_human_relationship_status(rel) for rel in records}
    batched = dict(
        zip(
            (rel.description for rel in records),
            await evaluate_human_relationship_statuses(records),
            strict=True,
        )
    )

    assert per_row == wanted
    assert batched == per_row


@dataclass(slots=True)
class Network:
    """A shared source agent whose contacts and peers all rest on the same three deciders."""

    data: RelationshipDatabase
    source: AgentRecord
    direct: UserRecord
    departmental: UserRecord
    outsider: UserRecord
    size: int = 0


async def build_network(data: RelationshipDatabase) -> Network:
    direct = await add_user(data.tenant_id, "Direct")
    departmental = await add_user(data.tenant_id, "Departmental")
    outsider = await add_user(data.tenant_id, "Outsider")
    source = await add_agent(data.tenant_id, data.actor.id, "Network source")
    await grant_user(source.id, direct.id, "manage")
    await grant_department(data.tenant_id, source.id, departmental.id)
    return Network(data, source, direct, departmental, outsider)


async def grow(network: Network, size: int) -> None:
    """Give the source agent size contacts and size peers, cycling through the three deciders."""
    data, source = network.data, network.source
    while network.size < size:
        index = network.size
        slot = index % 3
        decider = (network.direct, network.departmental, network.outsider)[slot]
        member = await add_member(data.tenant_id, f"Contact {index}", user_id=decider.id)
        _ = await agent_relationship_dao.create(
            obj_in={
                "agent_id": source.id,
                "member_id": member.id,
                "relation": "collaborator",
                "description": "",
                "created_by_user_id": data.actor.id,
            }
        )
        created_by: UserRecord | None = None
        if slot == 2:
            peer = await add_agent(data.tenant_id, network.direct.id, f"Peer {index}", mode="private")
        else:
            peer = await add_agent(data.tenant_id, data.actor.id, f"Peer {index}")
            created_by = network.direct if slot == 0 else network.outsider
            if slot == 0:
                await grant_user(peer.id, network.direct.id, "manage")
        _ = await agent_agent_relationship_dao.create(
            obj_in={
                "agent_id": source.id,
                "target_agent_id": peer.id,
                "relation": "peer",
                "description": "",
                "created_by_user_id": created_by.id if created_by else None,
            }
        )
        network.size += 1


async def test_relationship_reads_cost_the_same_statements_for_three_and_thirty_rows(
    relationship_database: RelationshipDatabase,
    access_cache_unavailable: None,
    statements: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = relationship_database
    network = await build_network(data)
    source = network.source
    # Reconciliation has its own fixed statements and would add rows; it is not what is measured here.
    monkeypatch.setattr(relationships, "ensure_access_granted_platform_relationships", AsyncMock(return_value=False))
    monkeypatch.setattr(gateway, "_get_agent_by_key", AsyncMock(return_value=source))
    base = f"/api/agents/{source.id}/relationships"

    async def read(path: str, **params: Any) -> None:
        response = await data.client.get(base + path, params=params)
        assert response.status_code == 200
        payload = response.json()
        rows = payload["items"] if isinstance(payload, dict) else payload
        assert len(rows) == network.size
        assert {row["access_status"] for row in rows} == {"active", "restricted"}

    async def context() -> None:
        text = await _load_relationships_from_db(source.id)
        assert "### Contact 0 - " in text
        assert "### Peer 0 - " in text
        assert "### Contact 2 - " not in text
        assert "### Peer 1 - " not in text

    async def send() -> None:
        with pytest.raises(HTTPException) as error:
            await gateway.send_message(GatewaySendMessageRequest(target="nobody", content="Hi"), "api-key")
        assert error.value.status_code == 404

    surfaces: dict[str, Callable[[], Awaitable[None]]] = {
        "human list": lambda: read("/"),
        "human page": lambda: read("/page", limit=200),
        "agent list": lambda: read("/agents"),
        "agent context": context,
        "gateway send": send,
    }
    counts: dict[str, list[int]] = {name: [] for name in surfaces}
    per_row_human: list[int] = []
    per_row_agent: list[int] = []
    for size in (3, 30):
        await grow(network, size)
        for name, surface in surfaces.items():
            statements.clear()
            await surface()
            counts[name].append(len(statements))
        # Control: the per-row evaluators do grow on these rows, so equal counts above are not vacuous.
        contacts = await agent_relationship_dao.list_for_agent_with_members(source.id)
        statements.clear()
        for contact in contacts:
            _ = await evaluate_human_relationship_status(contact, source_agent=source)
        per_row_human.append(len(statements))
        peers = await agent_agent_relationship_dao.list_for_agent_with_targets(source.id)
        statements.clear()
        for peer in peers:
            _ = await evaluate_agent_relationship_status(peer, current_user_id=data.actor.id)
        per_row_agent.append(len(statements))

    for name, (small, large) in counts.items():
        assert small == large, f"{name}: {small} statements for 3 rows, {large} for 30"
    assert per_row_human[0] < per_row_human[1]
    assert per_row_agent[0] < per_row_agent[1]


@pytest.mark.parametrize("change", ["grant revoked", "user deactivated", "agent made private behind the row memo"])
async def test_a_change_between_two_human_evaluations_in_one_task_is_seen_by_the_second(
    relationship_database: RelationshipDatabase, access_cache_unavailable: None, change: str
) -> None:
    data = relationship_database
    cast = await build_cast(data)
    source = await shared_agent(cast, "Source", use=[cast.reader])
    member = await add_member(cast.tenant, "Contact", user_id=cast.reader.id)
    _ = await agent_relationship_dao.create(
        obj_in={
            "agent_id": source.id,
            "member_id": member.id,
            "relation": "collaborator",
            "description": "",
            "created_by_user_id": cast.admin.id,
        }
    )
    rels = await agent_relationship_dao.list_for_agent_with_members(source.id)
    assert await evaluate_human_relationship_statuses(rels) == [ACTIVE]

    if change == "grant revoked":
        grant = next(
            row for row in await agent_permission_dao.list_for_agent(source.id) if row.scope_id == cast.reader.id
        )
        _ = await agent_permission_dao.delete(record_id=grant.id)
    elif change == "user deactivated":
        _ = await user_dao.update(db_obj=cast.reader, obj_in={"is_active": False})
    else:
        _ = await agent_dao.get(source.id)
        async with connection_ctx() as db:
            await db.execute("UPDATE agents SET access_mode = 'private' WHERE id = %(id)s", {"id": source.id})
        memoized = await agent_dao.get(source.id)
        assert memoized is not None
        assert memoized.access_mode == AgentSharingMode.CUSTOM

    assert await evaluate_human_relationship_statuses(rels) == [NO_PLATFORM_ACCESS]


async def test_a_grant_revoked_between_two_agent_evaluations_is_seen_by_the_second(
    relationship_database: RelationshipDatabase, access_cache_unavailable: None
) -> None:
    data = relationship_database
    cast = await build_cast(data)
    source = await shared_agent(cast, "Source", manage=[cast.manager])
    target = await shared_agent(cast, "Target", manage=[cast.manager])
    _ = await agent_agent_relationship_dao.create(
        obj_in={
            "agent_id": source.id,
            "target_agent_id": target.id,
            "relation": "peer",
            "description": "",
            "created_by_user_id": cast.manager.id,
        }
    )
    rels = await agent_agent_relationship_dao.list_for_agent_with_targets(source.id)
    assert await evaluate_agent_relationship_statuses(rels) == [ACTIVE]

    grant = next(row for row in await agent_permission_dao.list_for_agent(target.id) if row.scope_id == cast.manager.id)
    _ = await agent_permission_dao.delete(record_id=grant.id)

    assert await evaluate_agent_relationship_statuses(rels) == [CREATOR_LOST]


async def test_batched_agent_statuses_ignore_the_preloaded_target_row(
    relationship_database: RelationshipDatabase, access_cache_unavailable: None
) -> None:
    data = relationship_database
    cast = await build_cast(data)
    source = await shared_agent(cast, "Source")
    target = await shared_agent(cast, "Target")
    _ = await agent_agent_relationship_dao.create(
        obj_in={"agent_id": source.id, "target_agent_id": target.id, "relation": "peer", "description": ""}
    )
    rels = await agent_agent_relationship_dao.list_for_agent_with_targets(source.id)
    async with connection_ctx() as db:
        await db.execute("UPDATE agents SET status = 'stopped' WHERE id = %(id)s", {"id": target.id})

    assert rels[0].target_agent is not None
    assert rels[0].target_agent.status != "stopped"
    assert await evaluate_agent_relationship_statuses(rels) == [expected("restricted", "target_status_stopped")]


async def test_batch_never_reads_or_fills_the_access_cache(
    relationship_database: RelationshipDatabase, access_cache_unavailable: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = relationship_database
    cast = await build_cast(data)
    source = await shared_agent(cast, "Source")
    member = await add_member(cast.tenant, "Contact", user_id=cast.outsider.id)
    rel = AgentRelationshipRecord(uuid4(), source.id, member.id, member=member)
    stale_allow = AsyncMock(return_value=PermissionAccessLevel.MANAGE)
    snapshot = AsyncMock(return_value=access_cache.NO_ACL_SNAPSHOT)
    fill = AsyncMock()
    monkeypatch.setattr(access_cache, "get_cached_level", stale_allow)
    monkeypatch.setattr(access_cache, "read_acl_snapshot", snapshot)
    monkeypatch.setattr(access_cache, "set_cached_level", fill)

    # The per-row evaluator serves the cached allow; the batch decides from SQL and writes nothing back.
    assert await evaluate_human_relationship_status(rel) == ACTIVE
    stale_allow.reset_mock()
    snapshot.reset_mock()
    assert await evaluate_human_relationship_statuses([rel]) == [NO_PLATFORM_ACCESS]

    stale_allow.assert_not_awaited()
    snapshot.assert_not_awaited()
    fill.assert_not_awaited()


async def test_batch_reads_take_no_row_locks_and_do_not_wait_on_the_gateway_agent_lock(
    relationship_database: RelationshipDatabase, access_cache_unavailable: None, statements: list[str]
) -> None:
    data = relationship_database
    network = await build_network(data)
    await grow(network, 6)
    contacts = await agent_relationship_dao.list_for_agent_with_members(network.source.id)
    peers = await agent_agent_relationship_dao.list_for_agent_with_targets(network.source.id)
    holding, finished = anyio.Event(), anyio.Event()
    issued: list[str] = []

    async def hold_the_gateway_locks() -> None:
        async with connection_ctx() as db:
            await lock_tenants(db, (data.tenant_id,))
            await lock_agent(db, network.source.id, data.tenant_id)
            holding.set()
            await finished.wait()

    async def evaluate_meanwhile() -> None:
        await holding.wait()
        statements.clear()
        with anyio.fail_after(10):
            human = await evaluate_human_relationship_statuses(contacts)
            agents = await evaluate_agent_relationship_statuses(peers)
        issued.extend(statements)
        assert len(human) == len(contacts)
        assert len(agents) == len(peers)
        finished.set()

    with anyio.fail_after(30):
        async with anyio.create_task_group() as group:
            group.start_soon(hold_the_gateway_locks)
            group.start_soon(evaluate_meanwhile)

    assert issued
    assert not [
        sql for sql in issued if re.search(r"\bFOR\s+(NO\s+KEY\s+|KEY\s+)?(UPDATE|SHARE)\b", sql, re.IGNORECASE)
    ]
