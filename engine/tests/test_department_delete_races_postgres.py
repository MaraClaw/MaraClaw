import anyio
import pytest
from httpx import Response
from psycopg import AsyncConnection
from psycopg.rows import DictRow, dict_row

from app.api import agents
from app.core.acl_locks import lock_tenants
from app.db.connection import DbConnection
from app.db.session import transaction
from app.services import org_departments
from department_delete_postgres_fixtures import DepartmentStore, department_store, request
from test_bootstrap_postgres import schema_db

__all__ = ["department_store", "schema_db"]


@pytest.mark.parametrize("mutation", ["create", "assign", "permission", "delete"])
@pytest.mark.parametrize("delete_first", [True, False])
async def test_delete_serializes_with_department_writers(
    department_store: DepartmentStore, monkeypatch: pytest.MonkeyPatch, mutation: str, delete_first: bool
) -> None:
    # Given two independent PostgreSQL connections competing for the tenant mutex.
    store = department_store
    first_operation, second_operation = ("delete", mutation) if delete_first else (mutation, "delete")
    locked, release = anyio.Event(), anyio.Event()
    responses: dict[str, Response] = {}
    async with (
        await AsyncConnection[DictRow].connect(store.conninfo, row_factory=dict_row) as first,
        await AsyncConnection[DictRow].connect(store.conninfo, row_factory=dict_row) as second,
    ):

        async def held_tenant_lock(db, ids) -> None:
            await lock_tenants(db, ids)
            if db.raw.info.backend_pid == first.info.backend_pid:
                locked.set()
                await release.wait()

        monkeypatch.setattr(org_departments, "lock_tenants", held_tenant_lock)
        monkeypatch.setattr(agents, "lock_tenants", held_tenant_lock)

        async def run(name: str, operation: str, raw: AsyncConnection[DictRow]) -> None:
            async with transaction(DbConnection(raw)):
                responses[name] = await request(store, operation)

        # When the second real endpoint is blocked behind the first, then allowed to proceed.
        with anyio.fail_after(15):
            async with anyio.create_task_group() as group:
                group.start_soon(run, "first", first_operation, first)
                await locked.wait()
                group.start_soon(run, "second", second_operation, second)
                try:
                    while True:
                        row = await store.db.fetchone(
                            "SELECT %(first)s = ANY(pg_blocking_pids(%(second)s)) AS blocked",
                            {"first": first.info.backend_pid, "second": second.info.backend_pid},
                        )
                        assert row is not None
                        if row["blocked"] is True:
                            break
                finally:
                    release.set()
        # Then both orderings terminate without deadlocks or dangling department references.
        success = {"create": 201, "assign": 200, "permission": 200, "delete": 204}
        assert responses["first"].status_code == success[first_operation], responses["first"].text
        expected_second = (404 if mutation == "delete" else 422) if delete_first else 204
        if mutation == "delete":
            expected_second = 404
        assert responses["second"].status_code == expected_second, responses["second"].text
    assert await store.db.fetchall("SELECT id FROM local_departments WHERE id = %(id)s", {"id": store.selected}) == []
    assert (
        await store.db.fetchall(
            "SELECT user_id FROM local_department_memberships WHERE department_id = %(id)s", {"id": store.selected}
        )
        == []
    )
    assert (
        await store.db.fetchall(
            "SELECT id FROM agent_permissions WHERE scope_type = 'department' AND scope_id = %(id)s",
            {"id": store.selected},
        )
        == []
    )
    created = await store.db.fetchall("SELECT id FROM agents WHERE name = 'Created'")
    assert len(created) == int(mutation == "create" and not delete_first)
    if mutation == "assign" and not delete_first:
        assert await store.db.fetchall(
            "SELECT department_id FROM local_department_memberships WHERE user_id = %(id)s",
            {"id": store.members[0]},
        ) == [{"department_id": store.other}]
