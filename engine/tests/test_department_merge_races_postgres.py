import anyio
import pytest
from httpx import Response
from psycopg import AsyncConnection
from psycopg.rows import DictRow, dict_row

from app.api import agents
from app.core.acl_locks import lock_tenants
from app.db.connection import DbConnection
from app.db.session import transaction
from app.services import agent_provisioning, org_departments
from department_merge_postgres_fixtures import (
    department_store,
    merge_request,
    merge_state,
    merge_store,
    schema_db,
)

__all__ = ["department_store", "merge_store", "schema_db"]


@pytest.mark.parametrize(
    "mutation",
    [
        "rename",
        "destination_rename",
        "delete",
        "destination_delete",
        "assign",
        "permission",
        "create",
        "reverse_merge",
    ],
)
@pytest.mark.parametrize("merge_first", [True, False])
async def test_merge_serializes_with_department_and_permission_writers(merge_store, monkeypatch, mutation, merge_first):
    store, _, unrelated = merge_store
    first_operation, second_operation = ("merge", mutation) if merge_first else (mutation, "merge")
    locked, release = anyio.Event(), anyio.Event()
    responses: dict[str, Response] = {}
    async with (
        await AsyncConnection[DictRow].connect(store.conninfo, row_factory=dict_row) as first,
        await AsyncConnection[DictRow].connect(store.conninfo, row_factory=dict_row) as second,
    ):

        async def held_tenant_lock(db, ids):
            await lock_tenants(db, ids)
            if db.raw.info.backend_pid == first.info.backend_pid:
                locked.set()
                await release.wait()

        monkeypatch.setattr(org_departments, "lock_tenants", held_tenant_lock)
        monkeypatch.setattr(agents, "lock_tenants", held_tenant_lock)
        monkeypatch.setattr(agent_provisioning, "lock_tenants", held_tenant_lock)

        async def run(name: str, operation: str, raw: AsyncConnection[DictRow]):
            async with transaction(DbConnection(raw)):
                responses[name] = await merge_request(store, operation)

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

        success = {
            "merge": 200,
            "rename": 200,
            "destination_rename": 200,
            "delete": 204,
            "destination_delete": 204,
            "assign": 200,
            "permission": 200,
            "create": 201,
            "reverse_merge": 200,
        }
        assert responses["first"].status_code == success[first_operation], responses["first"].text
        expected = 200
        if merge_first:
            match mutation:
                case "rename" | "delete" | "reverse_merge":
                    expected = 404
                case "assign" | "permission" | "create":
                    expected = 422
                case "destination_delete":
                    expected = 204
        elif mutation in {"delete", "destination_delete", "reverse_merge"}:
            expected = 404
        assert responses["second"].status_code == expected, responses["second"].text

    state = await merge_state(store)
    department_ids = {row["id"] for row in state[0]}
    assert unrelated in department_ids
    assert all(row["department_id"] in department_ids for row in state[1])
    assert all(row["scope_id"] in department_ids for row in state[2] if row["scope_type"] == "department")
    if mutation == "destination_rename":
        assert next(row["name"] for row in state[0] if row["id"] == store.other) == "Renamed"
        if not merge_first:
            assert responses["second"].json()["destination"]["name"] == "Renamed"
    if mutation == "assign" and not merge_first:
        assert {row["department_id"] for row in state[1] if row["user_id"] == store.members[0]} == {store.other}
    events = await store.db.fetchall("SELECT action FROM admin_audit_logs WHERE action = 'department.merge'")
    expected_merges = int(merge_first or mutation not in {"delete", "destination_delete"})
    assert len(events) == expected_merges
