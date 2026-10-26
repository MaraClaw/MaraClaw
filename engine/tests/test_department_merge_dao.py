from datetime import UTC, datetime
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from psycopg import AsyncConnection
from psycopg.errors import ForeignKeyViolation

from app.dao.org_department_dao import OrgDepartmentDAO
from app.db.connection import DbConnection
from app.db.errors import ForeignKeyViolationError
from app.db.session import get_connection, transaction
from app.records.org_department import OrgDepartmentRecord


async def test_merge_dao_copies_deduplicated_associations_before_cleanup_in_one_transaction():
    raw = AsyncMock(spec=AsyncConnection)
    cursor = raw.cursor.return_value.__aenter__.return_value
    source = OrgDepartmentRecord(id=uuid4(), tenant_id=uuid4(), name="Source", created_at=datetime.now(UTC))
    destination = OrgDepartmentRecord(
        id=uuid4(), tenant_id=source.tenant_id, name="Destination", created_at=source.created_at
    )
    async with transaction(DbConnection(raw)):
        await OrgDepartmentDAO().merge_departments(source, destination)
        raw.commit.assert_not_awaited()
    statements = [call.args[0] for call in cursor.execute.await_args_list]
    assert len(statements) == 5
    assert statements[0].startswith("INSERT INTO local_department_memberships")
    assert "ON CONFLICT (user_id, department_id) DO NOTHING" in statements[0]
    assert statements[1].startswith("INSERT INTO agent_permissions")
    assert "a.access_mode = 'custom'" in statements[1]
    assert "p.access_level = 'use'" in statements[1]
    assert "ON CONFLICT (agent_id, scope_id) WHERE scope_type = 'department' DO NOTHING" in statements[1]
    assert all(statement.startswith("DELETE FROM") for statement in statements[2:])
    raw.commit.assert_awaited_once()
    raw.rollback.assert_not_awaited()
    assert get_connection() is None


@pytest.mark.parametrize("failed_statement", range(5))
async def test_merge_dao_sql_failure_aborts_owning_transaction(failed_statement):
    raw = AsyncMock(spec=AsyncConnection)
    cursor = raw.cursor.return_value.__aenter__.return_value
    cursor.execute.side_effect = [None] * failed_statement + [ForeignKeyViolation()]
    source = OrgDepartmentRecord(id=uuid4(), tenant_id=uuid4(), name="Source", created_at=datetime.now(UTC))
    destination = OrgDepartmentRecord(
        id=uuid4(), tenant_id=source.tenant_id, name="Destination", created_at=source.created_at
    )
    with pytest.raises(ForeignKeyViolationError):
        async with transaction(DbConnection(raw)):
            await OrgDepartmentDAO().merge_departments(source, destination)
    raw.commit.assert_not_awaited()
    raw.rollback.assert_awaited_once()
    assert get_connection() is None
