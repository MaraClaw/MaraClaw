"""Transactional SQL fake for LLM audit tests; real DAOs and audit writer remain in use."""

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import asdict
from typing import final, override
from uuid import UUID, uuid4

import pytest
from psycopg.types.json import Jsonb
from tests.test_enterprise_llm import _model
from tests.transaction_pool_fakes import CountingPool, CountingRawConnection

from app.api import enterprise
from app.core.domain.users import UserRole
from app.core.json_types import object_attr
from app.db import session
from app.db.connection import DbConnection, Params, Row
from app.records.tenant import TenantRecord
from app.records.user import UserRecord


@final
class AuditDatabase(CountingRawConnection):
    def __init__(self) -> None:
        super().__init__()
        self.model = _model(base_url="https://url-user:url-password@provider.invalid/v1?token=url-token#url-fragment")
        assert self.model.tenant_id is not None
        self.tenant = TenantRecord(id=self.model.tenant_id, name="Company", slug="company")
        self.actor = UserRecord(id=uuid4(), tenant_id=self.tenant.id, role=UserRole.ORG_ADMIN)
        self.rows: dict[str, dict[UUID, Row]] = {
            "llm_models": {self.model.id: asdict(self.model)},
            "tenants": {self.tenant.id: asdict(self.tenant)},
            "admin_audit_logs": {},
        }
        self._before = deepcopy(self.rows)
        self._savepoint = deepcopy(self.rows)
        self.statements: list[str] = []
        self.reject_audit = False
        self.failed = False
        self.pool = CountingPool()
        self.pool.raw = self

    @property
    def events(self) -> list[Row]:
        return list(self.rows["admin_audit_logs"].values())

    @override
    def begin(self) -> None:
        self._before = deepcopy(self.rows)
        super().begin()

    @override
    async def commit(self) -> None:
        assert not self.failed, "an SQL failure must be recovered before commit"
        await super().commit()

    @override
    async def rollback(self) -> None:
        self.rows = deepcopy(self._before)
        self.failed = False
        await super().rollback()

    def query(self, query: str, params: Params) -> Row | None:
        self.statements.append(query)
        if query == "ROLLBACK TO SAVEPOINT admin_audit_write":
            self.rows = deepcopy(self._savepoint)
            self.failed = False
            return None
        assert not self.failed, "transaction is aborted until savepoint rollback"
        if query == "SAVEPOINT admin_audit_write":
            self._savepoint = deepcopy(self.rows)
            return None
        if query == "RELEASE SAVEPOINT admin_audit_write":
            return None
        assert isinstance(params, Mapping)
        data = {
            str(key): object_attr(value, "obj") if isinstance(value, Jsonb) else value for key, value in params.items()
        }
        row_id = data["id"]
        assert isinstance(row_id, UUID)
        if query.startswith("SELECT "):
            table = query.split(" FROM ", 1)[1].split()[0]
            return deepcopy(self.rows[table].get(row_id))
        if query.startswith("INSERT INTO "):
            table = query.split()[2]
            if table == "admin_audit_logs" and self.reject_audit:
                self.failed = True
                raise RuntimeError("audit insert rejected")
            data.setdefault("created_at", self.model.created_at)
            self.rows[table][row_id] = deepcopy(data)
            return deepcopy(data)
        if query.startswith("UPDATE "):
            table = query.split()[1]
            self.rows[table][row_id].update(deepcopy(data))
            return deepcopy(self.rows[table][row_id])
        assert query.startswith("DELETE FROM llm_models ")
        deleted = self.rows["llm_models"].pop(row_id, None)
        for tenant in self.rows["tenants"].values():
            for slot in ("default_model_id", "default_secondary_model_id", "default_fallback_model_id"):
                if tenant[slot] == row_id:
                    tenant[slot] = None
        return deepcopy(deleted)


@pytest.fixture
def audit_db(monkeypatch: pytest.MonkeyPatch) -> AuditDatabase:
    database = AuditDatabase()

    async def execute(db: DbConnection, query: str, params: Params = None) -> None:
        assert db.raw is database
        database.query(query, params)

    async def fetchone(db: DbConnection, query: str, params: Params = None) -> Row | None:
        assert db.raw is database
        return database.query(query, params)

    async def agent_mutation(**kwargs: object) -> int:
        return 0

    async def references(model_id: UUID) -> list[str]:
        return []

    for method in (
        "migrate_primary_model",
        "migrate_secondary_model",
        "migrate_fallback_model",
        "clear_other_slots_matching",
    ):
        monkeypatch.setattr(enterprise.agent_dao, method, agent_mutation)
    monkeypatch.setattr(enterprise.agent_dao, "names_referencing_model", references)
    monkeypatch.setattr(session, "get_pool", lambda: database.pool)
    monkeypatch.setattr(DbConnection, "execute", execute)
    monkeypatch.setattr(DbConnection, "fetchone", fetchone)
    return database
