from collections.abc import Mapping
from copy import deepcopy
from typing import override

from psycopg.types.json import Jsonb

from app.core.json_types import object_attr
from app.db.connection import DbConnection, Params, Row


class MemoryPolicyConnection(DbConnection):
    def __init__(self, value: dict[str, object] | None = None) -> None:
        self.value: dict[str, object] | None = deepcopy(value)
        self.audits: list[Row] = []
        self.statements: list[tuple[str, Params]] = []
        self.events: list[str] = []
        self.update_error: RuntimeError | None = None
        self.committed_value: dict[str, object] | None = deepcopy(value)
        self.committed_audits: list[Row] = []

    @staticmethod
    def _params(params: Params) -> Row:
        assert isinstance(params, Mapping)
        return {key: object_attr(value, "obj") if isinstance(value, Jsonb) else value for key, value in params.items()}

    @override
    async def execute(self, query: str, params: Params = None) -> None:
        self.statements.append((query, params))
        if query.startswith("INSERT INTO system_settings"):
            assert "VALUES (%(key)s, %(value)s) ON CONFLICT (key) DO NOTHING" in query
            values = self._params(params)
            assert values["key"] == "transfer_policy"
            value = values["value"]
            assert isinstance(value, dict)
            if self.value is None:
                self.value = dict(value)
            self.events.append("seed")
            return
        assert query.startswith(("SAVEPOINT admin_audit_write", "RELEASE SAVEPOINT admin_audit_write"))

    @override
    async def fetchone(self, query: str, params: Params = None) -> Row | None:
        self.statements.append((query, params))
        values = self._params(params)
        if query.startswith("SELECT"):
            assert "FROM system_settings WHERE key = %(key)s" in query
            assert values["key"] == "transfer_policy"
            self.events.append("lock" if query.endswith(" FOR UPDATE") else "read")
            return {"key": "transfer_policy", "value": deepcopy(self.value)} if self.value is not None else None
        if query.startswith("UPDATE system_settings"):
            self.events.append("update")
            if self.update_error is not None:
                raise self.update_error
            value = values["value"]
            assert isinstance(value, dict)
            self.value = dict(value)
            return {"key": "transfer_policy", "value": deepcopy(self.value)}
        assert query.startswith("INSERT INTO admin_audit_logs")
        self.events.append("audit")
        self.audits.append(values)
        return values

    @override
    async def commit(self) -> None:
        self.events.append("commit")
        self.committed_value = deepcopy(self.value)
        self.committed_audits = deepcopy(self.audits)

    @override
    async def rollback(self) -> None:
        self.events.append("rollback")
        self.value = deepcopy(self.committed_value)
        self.audits = deepcopy(self.committed_audits)
