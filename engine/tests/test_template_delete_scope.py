import uuid
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.api import advanced
from app.dao.template_dao import agent_template_dao
from app.dao.user_dao import user_dao
from app.records.identity import IdentityRecord
from app.records.template import AgentTemplateRecord
from app.records.user import UserRecord


@pytest.mark.parametrize("case", ["own", "foreign", "builtin", "no_creator", "missing_creator", "tenantless"])
@pytest.mark.parametrize("platform", [False, True])
async def test_delete_template_confines_org_admin_to_non_builtin_own_tenant_creators(monkeypatch, case, platform):
    actor = UserRecord(
        id=uuid.uuid4(),
        tenant_id=None if case == "tenantless" else uuid.uuid4(),
        role="platform_admin" if platform else "org_admin",
    )
    creator = UserRecord(id=uuid.uuid4(), tenant_id=uuid.uuid4() if case == "foreign" else actor.tenant_id)
    template = AgentTemplateRecord(
        id=uuid.uuid4(),
        name="Template",
        is_builtin=case == "builtin",
        created_by=None if case == "no_creator" else creator.id,
    )
    lookup = AsyncMock(return_value=None if case == "missing_creator" else creator)
    deletion = AsyncMock(return_value=True)
    monkeypatch.setattr(agent_template_dao, "get", AsyncMock(return_value=template))
    monkeypatch.setattr(agent_template_dao, "delete", deletion)
    monkeypatch.setattr(user_dao, "get", lookup)

    if platform or case == "own":
        await advanced.delete_template(template.id, actor)
        deletion.assert_awaited_once_with(id=template.id)
        if platform:
            lookup.assert_not_awaited()
    else:
        with pytest.raises(HTTPException) as error:
            await advanced.delete_template(template.id, actor)
        assert error.value.status_code == (403 if case == "builtin" else 404)
        deletion.assert_not_awaited()


async def test_identity_platform_admin_can_delete_builtin_template(monkeypatch):
    actor = UserRecord(
        id=uuid.uuid4(), role="org_admin", identity=IdentityRecord(id=uuid.uuid4(), is_platform_admin=True)
    )
    template = AgentTemplateRecord(id=uuid.uuid4(), name="Builtin", is_builtin=True)
    deletion = AsyncMock(return_value=True)
    monkeypatch.setattr(agent_template_dao, "get", AsyncMock(return_value=template))
    monkeypatch.setattr(agent_template_dao, "delete", deletion)

    await advanced.delete_template(template.id, actor)

    deletion.assert_awaited_once_with(id=template.id)


async def test_delete_missing_template_preserves_not_found(monkeypatch):
    deletion = AsyncMock()
    monkeypatch.setattr(agent_template_dao, "get", AsyncMock(return_value=None))
    monkeypatch.setattr(agent_template_dao, "delete", deletion)
    actor = UserRecord(id=uuid.uuid4(), tenant_id=uuid.uuid4(), role="org_admin")

    with pytest.raises(HTTPException) as error:
        await advanced.delete_template(uuid.uuid4(), actor)

    assert error.value.status_code == 404
    deletion.assert_not_awaited()
