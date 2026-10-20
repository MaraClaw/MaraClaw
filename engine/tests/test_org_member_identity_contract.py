import re
import sqlite3
from collections.abc import AsyncGenerator, Iterator, Mapping
from contextlib import asynccontextmanager
from typing import Literal, assert_never
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from psycopg.sql import SQL, Identifier, Placeholder

from app.dao import agent_relationship_dao, identity_provider_dao, org_member_dao
from app.records.identity import IdentityProviderRecord, IdentityRecord
from app.records.user import UserRecord
from app.services.channel_user_service import ChannelUserService
from app.services.registration_service import RegistrationService
from app.services.sso_service import sso_service

type SqlValue = str | int | UUID | None

PROVIDER_ID = UUID(int=100)
TENANT_ID = UUID(int=200)
USER_ID = UUID(int=300)
AGENT_ID = UUID(int=400)


class IdentityStore:
    def __init__(self) -> None:
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript(
            "PRAGMA foreign_keys = ON;"
            "CREATE TABLE identity_providers (id TEXT PRIMARY KEY, name TEXT, provider_type TEXT);"
            "CREATE TABLE org_members ("
            "id TEXT PRIMARY KEY, tenant_id TEXT, user_id TEXT, "
            "provider_id TEXT REFERENCES identity_providers(id), "
            "open_id TEXT, unionid TEXT, external_id TEXT, name TEXT, name_translit_full TEXT, "
            "name_translit_initial TEXT, email TEXT, avatar_url TEXT, title TEXT DEFAULT '', "
            "phone TEXT, status TEXT DEFAULT 'active');"
            "CREATE TABLE agent_relationships (id TEXT PRIMARY KEY, agent_id TEXT, "
            "member_id TEXT REFERENCES org_members(id), relation TEXT, description TEXT, "
            "created_by_user_id TEXT, updated_by_user_id TEXT);"
            "CREATE TABLE users (id TEXT PRIMARY KEY, tenant_id TEXT, is_active BOOLEAN);"
        )
        self.insert("identity_providers", {"id": PROVIDER_ID, "name": "Feishu", "provider_type": "feishu"})

    def insert(self, table: str, values: Mapping[str, SqlValue]) -> None:
        statement = SQL("INSERT INTO {} ({}) VALUES ({})").format(
            Identifier(table),
            SQL(", ").join(Identifier(key) for key in values),
            SQL(", ").join(Placeholder(key) for key in values),
        )
        self.query(statement.as_string(), values)

    @staticmethod
    def parameters(values: Mapping[str, SqlValue]) -> dict[str, str | int | None]:
        return {key: str(value) if isinstance(value, UUID) else value for key, value in values.items()}

    def query(self, sql: str, params: Mapping[str, SqlValue]) -> sqlite3.Cursor:
        query = re.sub(r"%\((\w+)\)s", r":\1", sql).replace(" ILIKE ", " LIKE ")
        return self.connection.execute(query, self.parameters(params))

    async def fetchone(self, sql: str, params: Mapping[str, SqlValue]) -> dict[str, str | int | None] | None:
        row = self.query(sql, params).fetchone()
        return dict(row) if row is not None else None

    async def fetchall(self, sql: str, params: Mapping[str, SqlValue]) -> list[dict[str, str | int | None]]:
        return [dict(row) for row in self.query(sql, params).fetchall()]

    async def execute(self, sql: str, params: Mapping[str, SqlValue]) -> None:
        self.query(sql, params)

    @asynccontextmanager
    async def session(self) -> AsyncGenerator[IdentityStore]:
        yield self

    def member(self, member_id: int, **fields: SqlValue) -> UUID:
        identity = UUID(int=member_id)
        self.insert(
            "org_members",
            {"id": identity, "tenant_id": TENANT_ID, "provider_id": PROVIDER_ID, "name": "Employee", **fields},
        )
        return identity


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> Iterator[IdentityStore]:
    database = IdentityStore()
    for dao in (org_member_dao, agent_relationship_dao, identity_provider_dao):
        monkeypatch.setattr(dao, "session", database.session)
    try:
        yield database
    finally:
        database.connection.close()


@pytest.mark.parametrize("field", ["unionid", "open_id", "external_id"])
async def test_identity_lookup_prefers_linked_then_lowest_id_with_provider_isolation(store: IdentityStore, field: str):
    # Given
    store.insert("identity_providers", {"id": UUID(int=101)})
    store.member(0, provider_id=UUID(int=101), user_id=USER_ID, **{field: "shared"})
    store.member(1, **{field: "shared"})
    store.member(4, user_id=USER_ID, **{field: "shared"})
    expected = store.member(3, user_id=USER_ID, **{field: "shared"})
    store.member(2, status="inactive", user_id=USER_ID, **{field: "shared"})
    # When
    member = await org_member_dao.find_active_by_any_ids(provider_id=PROVIDER_ID, **{field: "shared"})
    # Then
    assert member is not None
    assert member.id == expected


async def test_channel_user_lookup_keeps_tenant_and_provider_scope(store: IdentityStore):
    # Given
    store.insert("identity_providers", {"id": UUID(int=101)})
    store.member(1, user_id=USER_ID, tenant_id=UUID(int=201))
    store.member(2, user_id=USER_ID, provider_id=UUID(int=101))
    expected = store.member(3, user_id=USER_ID)
    # When
    member = await org_member_dao.get_active_for_user_and_provider(
        user_id=USER_ID, provider_id=PROVIDER_ID, tenant_id=TENANT_ID
    )
    # Then
    assert member is not None
    assert member.id == expected


@pytest.mark.parametrize("search", ["Employee", "zhangsan", "zs"])
async def test_contact_search_preserves_name_transliteration_and_scope(store: IdentityStore, search: str):
    # Given
    store.insert("identity_providers", {"id": UUID(int=101)})
    fields = {"name_translit_full": "zhangsan", "name_translit_initial": "zs"}
    store.member(1, tenant_id=UUID(int=201), **fields)
    store.member(2, provider_id=UUID(int=101), **fields)
    store.member(3, status="inactive", **fields)
    expected = store.member(4, **fields)
    # When
    rows = await org_member_dao.list_active_filtered(tenant_id=TENANT_ID, provider_id=PROVIDER_ID, search=search)
    # Then
    assert [(member.id, name, kind) for member, name, kind in rows] == [(expected, "Feishu", "feishu")]


@pytest.mark.parametrize("surface", ["sso", "channel"])
async def test_auth_and_channel_lookup_preserve_member_identity(store: IdentityStore, surface: str):
    # Given
    expected = store.member(1, unionid="union-person", open_id="open-person", external_id="person", user_id=USER_ID)
    # When
    if surface == "sso":
        member = await sso_service._find_identity_member(
            PROVIDER_ID, "feishu", "open-person", {"raw_data": {"union_id": "union-person"}}
        )
    else:
        member = await ChannelUserService()._find_org_member(
            PROVIDER_ID, "feishu", "person", {"unionid": "union-person"}
        )
    # Then
    assert member is not None
    assert (member.id, member.user_id) == (expected, USER_ID)


@pytest.mark.parametrize("query", ["feishu", "members", "providers"])
async def test_relationship_joins_keep_relationship_and_member_ids(
    store: IdentityStore, query: Literal["feishu", "members", "providers"]
):
    # Given
    member_id = store.member(1, external_id="person", user_id=USER_ID)
    relation_id = UUID(int=500)
    store.insert("agent_relationships", {"id": relation_id, "agent_id": AGENT_ID, "member_id": member_id})
    # When
    match query:
        case "feishu":
            result = await agent_relationship_dao.get_active_for_agent_by_feishu_id(AGENT_ID, "person")
        case "members":
            result = (await agent_relationship_dao.list_for_agent_with_members(AGENT_ID))[0]
        case "providers":
            result = (await agent_relationship_dao.list_for_agent_with_members_and_providers(AGENT_ID))[0]
        case _:
            assert_never(query)
    # Then
    assert result is not None
    assert result.member is not None
    assert (result.id, result.member_id, result.member.id, result.member.user_id) == (
        relation_id,
        member_id,
        member_id,
        USER_ID,
    )


async def test_provider_deletion_keeps_member_and_relationship(store: IdentityStore):
    # Given
    member_id = store.member(1, external_id="person", user_id=USER_ID)
    relation_id = UUID(int=500)
    store.insert("agent_relationships", {"id": relation_id, "agent_id": AGENT_ID, "member_id": member_id})
    # When
    await identity_provider_dao.delete_nullifying_org_refs(PROVIDER_ID)
    # Then
    row = store.connection.execute(
        "SELECT m.id, m.user_id, m.provider_id, r.id AS relation_id "
        "FROM org_members m JOIN agent_relationships r ON r.member_id = m.id"
    ).fetchone()
    assert dict(row) == {
        "id": str(member_id),
        "user_id": str(USER_ID),
        "provider_id": None,
        "relation_id": str(relation_id),
    }
    assert store.connection.execute("SELECT id FROM identity_providers").fetchall() == []


@pytest.mark.parametrize("purpose", ["permissions", "okr", "relationships"])
async def test_permission_and_okr_membership_remains_tenant_scoped(store: IdentityStore, purpose: str):
    # Given
    store.insert("users", {"id": USER_ID, "tenant_id": TENANT_ID, "is_active": 1})
    expected = store.member(1, user_id=USER_ID)
    store.member(2, tenant_id=UUID(int=201), user_id=USER_ID)
    store.member(3, status="inactive", user_id=USER_ID)
    # When
    if purpose == "relationships":
        members = [member for member, _, _, _ in await org_member_dao.list_relationship_candidates(tenant_id=TENANT_ID)]
    elif purpose == "permissions":
        members = await org_member_dao.list_permission_candidates(tenant_id=TENANT_ID)
    else:
        members = await org_member_dao.list_active_with_user_for_tenant(TENANT_ID)
    # Then
    assert [member.id for member in members] == [expected]


@pytest.mark.parametrize("contact", ["email", "phone"])
async def test_registration_links_existing_member_and_propagates_contacts(
    store: IdentityStore, monkeypatch: pytest.MonkeyPatch, contact: str
):
    # Given
    member_id = store.member(1, email="employee@example.test", phone="123456")
    identity = IdentityRecord(
        id=UUID(int=600),
        email="employee@example.test" if contact == "email" else None,
        phone="123456" if contact == "phone" else None,
    )
    user = UserRecord(id=USER_ID, tenant_id=TENANT_ID, identity=identity)
    provider = IdentityProviderRecord(id=UUID(int=101), provider_type="web", name="Platform", tenant_id=TENANT_ID)
    store.insert("identity_providers", {"id": provider.id, "name": provider.name, "provider_type": "web"})
    service = RegistrationService()
    monkeypatch.setattr(service, "ensure_identity_provider", AsyncMock(return_value=provider))
    hook = AsyncMock()
    monkeypatch.setattr(service, "_run_okr_member_hook", hook)
    update = AsyncMock(return_value=identity)
    monkeypatch.setattr("app.services.registration_service.identity_dao.update", update)
    # When
    await service.bind_org_member(user)
    # Then
    row = store.connection.execute(
        "SELECT id, user_id FROM org_members WHERE provider_id = ?", (str(PROVIDER_ID),)
    ).fetchone()
    assert dict(row) == {"id": str(member_id), "user_id": str(USER_ID)}
    assert (user.email, user.primary_mobile) == ("employee@example.test", "123456")
    update.assert_awaited_once_with(
        db_obj=identity, obj_in={"phone": "123456"} if contact == "email" else {"email": "employee@example.test"}
    )
    hook.assert_any_await(member_id, TENANT_ID)


@pytest.mark.parametrize("linked", [False, True])
async def test_registration_reuses_platform_member_id(
    store: IdentityStore, monkeypatch: pytest.MonkeyPatch, linked: bool
):
    # Given
    member_id = store.member(1, email="employee@example.test", user_id=USER_ID if linked else None)
    user = UserRecord(
        id=USER_ID, tenant_id=TENANT_ID, identity=IdentityRecord(id=UUID(int=600), email="employee@example.test")
    )
    provider = IdentityProviderRecord(id=PROVIDER_ID, provider_type="web", name="Platform", tenant_id=TENANT_ID)
    service = RegistrationService()
    monkeypatch.setattr(service, "ensure_identity_provider", AsyncMock(return_value=provider))
    monkeypatch.setattr(service, "_run_okr_member_hook", AsyncMock())
    # When
    member = await service.ensure_web_org_member(user)
    # Then
    assert member is not None
    assert (member.id, member.user_id) == (member_id, USER_ID)
    assert store.connection.execute("SELECT COUNT(*) FROM org_members").fetchone()[0] == 1


async def test_contact_update_only_changes_linked_platform_members_in_same_tenant(
    store: IdentityStore, monkeypatch: pytest.MonkeyPatch
):
    # Given
    store.insert("identity_providers", {"id": UUID(int=101)})
    expected = store.member(1, user_id=USER_ID, email="old@example.test", phone="111")
    store.member(2, tenant_id=UUID(int=201), user_id=USER_ID)
    store.member(3, provider_id=UUID(int=101), user_id=USER_ID)
    store.member(4, user_id=UUID(int=301))
    user = UserRecord(
        id=USER_ID,
        tenant_id=TENANT_ID,
        identity=IdentityRecord(id=UUID(int=600), email="new@example.test", phone="222"),
    )
    provider = IdentityProviderRecord(id=PROVIDER_ID, provider_type="web", name="Platform", tenant_id=TENANT_ID)
    service = RegistrationService()
    monkeypatch.setattr(service, "ensure_identity_provider", AsyncMock(return_value=provider))
    # When
    await service.sync_org_member_contact_from_user(user, sync_email=True, sync_phone=True)
    # Then
    rows = store.connection.execute("SELECT id, email, phone FROM org_members ORDER BY id").fetchall()
    assert [tuple(row) for row in rows] == [
        (str(expected), "new@example.test", "222"),
        (str(UUID(int=2)), None, None),
        (str(UUID(int=3)), None, None),
        (str(UUID(int=4)), None, None),
    ]
