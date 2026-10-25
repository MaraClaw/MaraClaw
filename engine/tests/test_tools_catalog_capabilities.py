from collections.abc import Sequence
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from pydantic import TypeAdapter, ValidationError

from app.api import tools
from app.core.domain.users import UserRole
from app.core.json_types import JsonObject
from app.records.identity import IdentityRecord
from app.records.tool import ToolRecord
from app.records.user import UserRecord
from app.schemas.operator_response_tools import ToolCapabilities, ToolCatalogResponse
from app.services.tool_ownership import can_configure_tool, can_mutate_tool, tool_capabilities


@pytest.mark.parametrize("actor", ["org_admin", "tenantless_org_admin", "platform_admin", "identity_pa", "member"])
@pytest.mark.parametrize("scope", ["own", "global", "foreign", "builtin", "tagged_builtin", "builtin_type"])
def test_policy_matches_record_ownership_matrix(actor: str, scope: str) -> None:
    # Given real membership/identity records and each ownership class.
    tenant_id = uuid4()
    user = UserRecord(
        id=uuid4(),
        role=UserRole.MEMBER if actor in {"tenantless_org_admin", "identity_pa"} else UserRole(actor),
        tenant_id=tenant_id,
    )
    if actor == "tenantless_org_admin":
        user.role, user.tenant_id = UserRole.ORG_ADMIN, None
    elif actor == "identity_pa":
        user.role = UserRole.MEMBER
        user.identity = IdentityRecord(id=uuid4(), is_platform_admin=True)
    tool = ToolRecord(id=uuid4(), name="test", source="admin", type="mcp", tenant_id=tenant_id)
    if scope == "global":
        tool.tenant_id = None
    elif scope == "foreign":
        tool.tenant_id = uuid4()
    elif scope in {"builtin", "tagged_builtin"}:
        tool.source = "builtin"
        tool.tenant_id = None if scope == "builtin" else tenant_id
    elif scope == "builtin_type":
        tool.type = "builtin"
    expected: tuple[bool, bool, bool] = (False, False, False)
    if actor in {"platform_admin", "identity_pa"}:
        expected = (True, scope != "builtin_type", True)
    elif actor == "org_admin":
        expected = {
            "own": (True, True, True),
            "global": (False, False, False),
            "foreign": (False, False, False),
            "builtin": (False, False, True),
            "tagged_builtin": (False, False, True),
            "builtin_type": (True, False, True),
        }[scope]
    # When asking the pure policy for catalog capabilities.
    result = tool_capabilities(user, tool, user.tenant_id)
    # Then all mutation seams agree with the independently specified matrix.
    assert result == dict(zip(("can_edit_details", "can_delete", "can_configure"), expected, strict=True))
    assert can_mutate_tool(user, tool) == expected[0]
    assert can_configure_tool(user, tool, user.tenant_id) == expected[2]


@pytest.mark.parametrize("role", ["org_admin", "platform_admin", "member"])
@pytest.mark.parametrize("target", ["own", "foreign", "none"])
def test_builtin_configuration_requires_authorized_concrete_target(role: str, target: str) -> None:
    # Given a builtin whose malformed tenant tag must not grant ownership.
    user = UserRecord(id=uuid4(), tenant_id=uuid4(), role=UserRole(role))
    tool = ToolRecord(id=uuid4(), name="builtin", source="builtin", tenant_id=user.tenant_id)
    target_id = {"own": user.tenant_id, "foreign": uuid4(), "none": None}[target]
    # When evaluating configuration rights for the resolved target.
    result = tool_capabilities(user, tool, target_id)
    # Then only admins with an authorized concrete target may configure.
    assert result["can_configure"] == (
        target != "none" and (role == "platform_admin" or (role == "org_admin" and target == "own"))
    )


@pytest.fixture
def catalog(monkeypatch: pytest.MonkeyPatch) -> tuple[UserRecord, list[ToolRecord]]:
    user = UserRecord(id=uuid4(), tenant_id=uuid4(), role=UserRole.ORG_ADMIN)
    records = [
        ToolRecord(id=uuid4(), name="own", type="mcp", source="admin", tenant_id=user.tenant_id),
        ToolRecord(id=uuid4(), name="shared", type="mcp", source="admin"),
        ToolRecord(id=uuid4(), name="builtin", source="builtin"),
    ]

    async def list_catalog(tenant_id: UUID | None) -> list[ToolRecord]:
        return records

    async def builtin_configs(tenant_id: UUID | None, tools: Sequence[ToolRecord]) -> dict[str, JsonObject]:
        return {}

    async def row_config(tool: ToolRecord, tenant_id: UUID | None) -> JsonObject:
        return {}

    monkeypatch.setattr(tools.tool_dao, "list_platform_for_tenant", list_catalog)
    monkeypatch.setattr(tools, "get_tenant_tool_configs", builtin_configs)
    monkeypatch.setattr(tools, "get_tool_company_config", row_config)
    return user, records


@pytest.mark.parametrize("actor", ["org_admin", "member", "tenantless_org_admin", "platform_admin", "identity_pa"])
async def test_catalog_wire_exposes_required_ownership_and_capabilities(
    catalog: tuple[UserRecord, list[ToolRecord]], actor: str
) -> None:
    # Given a readable catalog and a real caller; PA selects a foreign target.
    user, records = catalog
    user.role = UserRole.MEMBER if actor in {"tenantless_org_admin", "identity_pa"} else UserRole(actor)
    params: dict[str, str] = {}
    if actor == "tenantless_org_admin":
        user.role, user.tenant_id = UserRole.ORG_ADMIN, None
    elif actor in {"platform_admin", "identity_pa"}:
        user.tenant_id = None
        params["tenant_id"] = str(uuid4())
        if actor == "identity_pa":
            user.role = UserRole.MEMBER
            user.identity = IdentityRecord(id=uuid4(), is_platform_admin=True)
    app = FastAPI()
    app.include_router(tools.router, prefix="/api")
    app.dependency_overrides[tools.get_current_user] = lambda: user
    # When reading the HTTP catalog.
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/tools", params=params)
    # Then existing keys survive and required typed ownership fields carry the correct rights.
    assert response.status_code == 200
    rows = TypeAdapter(list[ToolCatalogResponse]).validate_json(response.content)
    assert [row["tenant_id"] for row in rows] == [str(records[0].tenant_id), None, None]
    expected = {
        "org_admin": [(True, True, True), (False, False, False), (False, False, True)],
        "member": [(False, False, False)] * 3,
        "tenantless_org_admin": [(False, False, False)] * 3,
        "platform_admin": [(True, True, True), (True, True, True), (True, False, True)],
        "identity_pa": [(True, True, True), (True, True, True), (True, False, True)],
    }[actor]
    assert [tuple(row["capabilities"].values()) for row in rows] == expected
    assert {"id", "name", "config", "config_schema", "source", "created_at"} <= rows[0].keys()


@pytest.mark.parametrize("field", ["tenant_id", "capabilities"])
async def test_catalog_contract_rejects_missing_additive_fields(
    catalog: tuple[UserRecord, list[ToolRecord]], field: str
) -> None:
    # Given a valid catalog response with one required additive field removed.
    rows = await tools.list_tools(current_user=catalog[0])
    incomplete = dict(rows[0])
    incomplete.pop(field)
    # When validating the wire contract, then omission is rejected.
    with pytest.raises(ValidationError):
        TypeAdapter(ToolCatalogResponse).validate_python(incomplete)


@pytest.mark.parametrize("field", ["can_edit_details", "can_delete", "can_configure"])
def test_capability_contract_requires_every_boolean(field: str) -> None:
    # Given a capability object missing one boolean.
    incomplete = {"can_edit_details": False, "can_delete": False, "can_configure": False}
    incomplete.pop(field)
    # When validating capabilities, then omission is rejected.
    with pytest.raises(ValidationError):
        TypeAdapter(ToolCapabilities).validate_python(incomplete)
