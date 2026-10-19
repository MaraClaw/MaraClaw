import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.api import enterprise
from app.dao.audit_log_dao import AuditLogRecord, audit_log_dao
from app.records.identity import IdentityRecord
from app.records.user import UserRecord


@pytest.mark.parametrize("tenant_filter", ["omitted", "empty", "own"])
async def test_org_admin_audit_logs_keep_own_tenant_with_agent_filter(monkeypatch, tenant_filter):
    actor = UserRecord(id=uuid.uuid4(), tenant_id=uuid.uuid4(), role="org_admin")
    agent_id = uuid.uuid4()
    log = AuditLogRecord(id=uuid.uuid4(), action="test", agent_id=agent_id, created_at=datetime(2026, 1, 1, tzinfo=UTC))
    listing = AsyncMock(return_value=[log])
    monkeypatch.setattr(audit_log_dao, "list_scoped", listing)
    tenant_id = str(actor.tenant_id) if tenant_filter == "own" else "" if tenant_filter == "empty" else None

    result = await enterprise.list_audit_logs(agent_id, tenant_id, 17, actor)

    assert [item.id for item in result] == [log.id]
    listing.assert_awaited_once_with(tenant_id=actor.tenant_id, agent_id=agent_id, limit=17)


async def test_org_admin_foreign_audit_tenant_is_refused_not_ignored(monkeypatch):
    actor = UserRecord(id=uuid.uuid4(), tenant_id=uuid.uuid4(), role="org_admin")
    listing = AsyncMock(return_value=[])
    monkeypatch.setattr(audit_log_dao, "list_scoped", listing)

    with pytest.raises(HTTPException) as error:
        await enterprise.list_audit_logs(tenant_id=str(uuid.uuid4()), current_user=actor)

    assert error.value.status_code == 403
    listing.assert_not_awaited()


@pytest.mark.parametrize("scope", ["own", "foreign", "unscoped"])
@pytest.mark.parametrize("identity_admin", [False, True])
async def test_platform_admin_audit_scope_is_unchanged(monkeypatch, scope, identity_admin):
    tenant_id = None if scope == "unscoped" else uuid.uuid4()
    actor = UserRecord(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        role="org_admin" if identity_admin else "platform_admin",
        identity=IdentityRecord(id=uuid.uuid4(), is_platform_admin=identity_admin),
    )
    selected = uuid.uuid4() if scope == "foreign" else tenant_id
    listing = AsyncMock(return_value=[])
    monkeypatch.setattr(audit_log_dao, "list_scoped", listing)

    assert await enterprise.list_audit_logs(tenant_id=str(selected) if selected else None, current_user=actor) == []

    listing.assert_awaited_once_with(tenant_id=selected, agent_id=None, limit=50)
