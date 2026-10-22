from collections.abc import AsyncGenerator, AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI

from app.api import tools
from app.core.json_types import JsonObject
from app.records.identity import IdentityRecord
from app.records.tool import ToolRecord
from app.records.user import UserRecord


@dataclass
class WriteState:
    user: UserRecord
    records: dict[str, ToolRecord]
    rows: list[tuple[UUID, dict[str, object]]] = field(default_factory=list)
    configs: list[tuple[UUID, str, JsonObject]] = field(default_factory=list)
    deletes: list[tuple[str, UUID]] = field(default_factory=list)


@pytest.fixture
def writes(monkeypatch: pytest.MonkeyPatch) -> WriteState:
    tenant_id = uuid4()
    state = WriteState(
        UserRecord(id=uuid4(), tenant_id=tenant_id, role="org_admin"),
        {
            label: ToolRecord(id=uuid4(), name=label, type="mcp", source=source, tenant_id=owner)
            for label, source, owner in [
                ("own", "admin", tenant_id),
                ("global", "admin", None),
                ("foreign", "admin", uuid4()),
                ("builtin", "builtin", None),
                ("tagged_builtin", "builtin", tenant_id),
            ]
        },
    )

    async def get(tool_id: UUID) -> ToolRecord | None:
        return next((record for record in state.records.values() if record.id == tool_id), None)

    async def list_by_ids(ids: Sequence[UUID]) -> list[ToolRecord]:
        return [record for record in state.records.values() if record.id in ids]

    async def update(*, db_obj: ToolRecord, obj_in: Mapping[str, object]) -> ToolRecord:
        state.rows.append((db_obj.id, dict(obj_in)))
        return db_obj

    async def set_config(tenant_id: UUID, name: str, config: JsonObject, schema: Mapping[str, object]) -> None:
        state.configs.append((tenant_id, name, config))

    async def delete_assignments(tool_id: UUID) -> None:
        state.deletes.append(("assignments", tool_id))

    async def delete(*, id: UUID) -> None:
        state.deletes.append(("record", id))

    @asynccontextmanager
    async def transaction() -> AsyncGenerator[None]:
        yield

    monkeypatch.setattr(tools.tool_dao, "get", get)
    monkeypatch.setattr(tools.tool_dao, "list_by_ids", list_by_ids)
    monkeypatch.setattr(tools.tool_dao, "update", update)
    monkeypatch.setattr(tools.tool_dao, "delete", delete)
    monkeypatch.setattr(tools.agent_tool_dao, "delete_for_tool", delete_assignments)
    monkeypatch.setattr(tools, "set_tenant_tool_config", set_config)
    monkeypatch.setattr(tools, "connection_ctx", transaction)
    return state


@pytest.fixture
async def client(writes: WriteState) -> AsyncIterator[httpx.AsyncClient]:
    app = FastAPI()
    app.include_router(tools.router, prefix="/api")
    app.dependency_overrides[tools.get_current_user] = lambda: writes.user
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as session:
        yield session


@pytest.mark.parametrize("scope", ["global", "foreign", "builtin", "tagged_builtin"])
@pytest.mark.parametrize("payload", [{"enabled": False}, {"config": {"url": "new"}, "display_name": "new"}])
async def test_org_admin_update_rejects_unowned_record_before_writes(
    client: httpx.AsyncClient, writes: WriteState, scope: str, payload: JsonObject
) -> None:
    # Given an org admin and a shared or foreign record.
    record = writes.records[scope]
    # When metadata (possibly with config) is submitted.
    response = await client.put(f"/api/tools/{record.id}", json=payload)
    # Then the entire request is denied without any partial write.
    assert (response.status_code, writes.rows, writes.configs, writes.deletes) == (403, [], [], [])


@pytest.mark.parametrize("scope", ["global", "foreign"])
async def test_org_admin_nonbuiltin_config_requires_record_rights(
    client: httpx.AsyncClient, writes: WriteState, scope: str
) -> None:
    # Given config stored on a shared or foreign non-builtin row.
    record = writes.records[scope]
    # When an org admin attempts config-only replacement.
    response = await client.put(f"/api/tools/{record.id}", json={"config": {"url": "new"}})
    # Then the row is untouched.
    assert (response.status_code, writes.rows, writes.configs) == (403, [], [])


@pytest.mark.parametrize("scope", ["own", "builtin", "tagged_builtin"])
async def test_org_admin_config_replacement_uses_correct_storage(
    client: httpx.AsyncClient, writes: WriteState, scope: str
) -> None:
    # Given an owned row or builtin config, with values omitted by the replacement.
    record = writes.records[scope]
    record.config = {"old": "discard", "url": "old"}
    # When replacing config, not merging it.
    response = await client.put(f"/api/tools/{record.id}", json={"config": {"url": "new", "blank": ""}})
    # Then only the replacement is persisted at the correct boundary.
    assert response.status_code == 200
    if scope == "own":
        assert writes.rows == [(record.id, {"config": {"url": "new"}})]
        assert writes.configs == []
    else:
        assert writes.configs == [(writes.user.tenant_id, record.name, {"url": "new"})]
        assert writes.rows == []


@pytest.mark.parametrize("scope", ["global", "foreign", "builtin", "tagged_builtin"])
async def test_bulk_authorizes_all_records_before_updating_owned_first_item(
    client: httpx.AsyncClient, writes: WriteState, scope: str
) -> None:
    # Given an owned first item followed by a forbidden item.
    items = [writes.records["own"], writes.records[scope]]
    # When toggling the batch.
    response = await client.put("/api/tools/bulk", json=[{"tool_id": str(t.id), "enabled": False} for t in items])
    # Then no earlier item has been written.
    assert (response.status_code, writes.rows, writes.configs) == (403, [], [])


async def test_bulk_skips_missing_ids_and_updates_owned_records(client: httpx.AsyncClient, writes: WriteState) -> None:
    # Given one owned and one missing ID.
    record = writes.records["own"]
    # When toggling both.
    response = await client.put(
        "/api/tools/bulk", json=[{"tool_id": str(id), "enabled": False} for id in [uuid4(), record.id]]
    )
    # Then the missing ID is silently skipped.
    assert response.status_code == 200
    assert writes.rows == [(record.id, {"enabled": False})]


@pytest.mark.parametrize("scope", ["global", "foreign", "builtin", "tagged_builtin"])
async def test_org_admin_delete_denies_before_assignment_cleanup(
    client: httpx.AsyncClient, writes: WriteState, scope: str
) -> None:
    # Given an unowned record with deletable MCP type (even if builtin source).
    record = writes.records[scope]
    # When deleting it.
    response = await client.delete(f"/api/tools/{record.id}")
    # Then neither assignments nor the catalog row are deleted.
    assert (response.status_code, writes.deletes) == (403, [])


@pytest.mark.parametrize("role", ["org_admin", "platform_admin"])
async def test_builtin_type_delete_preserves_400(client: httpx.AsyncClient, writes: WriteState, role: str) -> None:
    # Given an otherwise mutable record with builtin type.
    writes.user.role = role
    record = writes.records["own"]
    record.type = "builtin"
    # When deleting it.
    response = await client.delete(f"/api/tools/{record.id}")
    # Then the longstanding builtin-type restriction wins.
    assert (response.status_code, writes.deletes) == (400, [])


@pytest.mark.parametrize("operation", ["update", "config", "bulk", "delete"])
@pytest.mark.parametrize("actor", ["member", "tenantless_org_admin"])
async def test_actor_without_concrete_ownership_cannot_write(
    client: httpx.AsyncClient, writes: WriteState, operation: str, actor: str
) -> None:
    # Given a non-manager or an org admin whose tenant is NULL, like the shared row.
    writes.user.role = "member" if actor == "member" else "org_admin"
    writes.user.tenant_id = None
    record = writes.records["global"]
    # When attempting any catalog row mutation.
    if operation == "delete":
        response = await client.delete(f"/api/tools/{record.id}")
    elif operation == "bulk":
        response = await client.put("/api/tools/bulk", json=[{"tool_id": str(record.id), "enabled": False}])
    else:
        payload = {"config": {}} if operation == "config" else {"enabled": False}
        response = await client.put(f"/api/tools/{record.id}", json=payload)
    # Then every persistence boundary remains untouched.
    assert (response.status_code, writes.rows, writes.configs, writes.deletes) == (403, [], [], [])


@pytest.mark.parametrize("actor", ["platform_admin", "identity_platform_admin", "org_admin"])
@pytest.mark.parametrize("operation", ["update", "bulk", "delete"])
async def test_authorized_manager_preserves_catalog_mutations(
    client: httpx.AsyncClient, writes: WriteState, actor: str, operation: str
) -> None:
    # Given a PA (including identity-backed PA) or an OA's own record.
    if actor == "identity_platform_admin":
        writes.user.role = "member"
        writes.user.identity = IdentityRecord(id=uuid4(), is_platform_admin=True)
    else:
        writes.user.role = actor
    record = writes.records["own" if actor == "org_admin" else "foreign"]
    # When performing a permitted mutation.
    if operation == "delete":
        response = await client.delete(f"/api/tools/{record.id}")
    elif operation == "bulk":
        response = await client.put("/api/tools/bulk", json=[{"tool_id": str(record.id), "enabled": False}])
    else:
        response = await client.put(f"/api/tools/{record.id}", json={"enabled": False})
    # Then the requested persistence occurs.
    assert response.status_code == 200
    if operation == "delete":
        assert writes.deletes == [("assignments", record.id), ("record", record.id)]
    else:
        assert writes.rows == [(record.id, {"enabled": False})]


@pytest.mark.parametrize("actor", ["org_admin", "platform_admin", "identity_platform_admin"])
@pytest.mark.parametrize("target", ["foreign", "none"])
async def test_builtin_config_target_guard_precedes_writes(
    client: httpx.AsyncClient, writes: WriteState, actor: str, target: str
) -> None:
    # Given a concrete foreign target or a caller without any target tenant.
    writes.user.role = actor
    if actor == "identity_platform_admin":
        writes.user.role = "member"
        writes.user.identity = IdentityRecord(id=uuid4(), is_platform_admin=True)
    if target == "none":
        writes.user.tenant_id = None
    payload: JsonObject = {"config": {"url": "new"}}
    if target == "foreign":
        payload["tenant_id"] = str(uuid4())
    record = writes.records["builtin"]
    # When replacing builtin company configuration.
    response = await client.put(f"/api/tools/{record.id}", json=payload)
    # Then only a PA with a concrete target writes company-local config.
    status = 400 if target == "none" else 403 if actor == "org_admin" else 200
    assert response.status_code == status
    assert writes.rows == []
    if status == 200:
        assert writes.configs == [(UUID(str(payload["tenant_id"])), record.name, {"url": "new"})]
    else:
        assert writes.configs == []
