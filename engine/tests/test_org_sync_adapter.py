import asyncio
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import override

import pytest

from app.records.identity import AuthProviderType
from app.services.org_sync_adapter import (
    SYNC_ADAPTER_CLASSES,
    BaseOrgSyncAdapter,
    ExternalDepartment,
    ExternalUser,
    GoogleWorkspaceOrgSyncAdapter,
    build_department_path_map,
)


class _DummyAdapter(BaseOrgSyncAdapter):
    provider_type = "feishu"

    @property
    @override
    def api_base_url(self) -> str:
        return "https://example.com"

    @override
    async def get_access_token(self) -> str:
        return "token"

    @override
    async def fetch_departments(self) -> list[ExternalDepartment]:
        return []

    @override
    async def fetch_users(self, department_external_id: str) -> list[ExternalUser]:
        return []


class _SyncAdapterWithFailure(_DummyAdapter):
    def __init__(self):
        provider = SimpleNamespace(
            id=uuid.uuid4(),
            provider_type=AuthProviderType.FEISHU,
            name="Feishu",
            config={},
        )
        super().__init__(provider=provider)
        self.reconcile_called = False
        self.member_counts_updated = False
        self.failure_provider = provider

    @override
    async def _ensure_provider(self, db: object) -> SimpleNamespace:
        return self.failure_provider

    @override
    async def _upsert_department(self, db: object, provider: SimpleNamespace, dept: ExternalDepartment) -> None:
        return None

    @override
    async def _upsert_member(
        self, db: object, provider: SimpleNamespace, user: ExternalUser, department_external_id: str
    ) -> dict[str, bool]:
        raise ValueError("unionid is required")

    @override
    async def _reconcile(self, db: object, provider_id: uuid.UUID, sync_start: datetime) -> None:
        self.reconcile_called = True

    @override
    async def _update_member_counts(self, db: object, provider_id: uuid.UUID) -> None:
        self.member_counts_updated = True

    @override
    async def _rebuild_department_paths(self, db: object, provider_id: uuid.UUID) -> dict[uuid.UUID, str]:
        return {}

    @override
    async def _refresh_member_department_paths(self, db: object, provider_id: uuid.UUID) -> None:
        return None

    @override
    async def fetch_departments(self) -> list[ExternalDepartment]:
        return [ExternalDepartment(external_id="dept-1", name="Dept 1")]

    @override
    async def fetch_users(self, department_external_id: str) -> list[ExternalUser]:
        return [ExternalUser(external_id="user-1", name="Alice", unionid="")]


def test_validate_member_identifiers_requires_unionid_for_feishu():
    adapter = _DummyAdapter()
    provider = SimpleNamespace(provider_type=AuthProviderType.FEISHU, name="Feishu", config={})
    user = ExternalUser(external_id="ou_123", name="Alice", unionid="")

    with pytest.raises(ValueError, match="unionid is required"):
        adapter._validate_member_identifiers(provider, user)


def test_validate_member_identifiers_rejects_unionid_equal_to_external_id():
    adapter = _DummyAdapter()
    provider = SimpleNamespace(provider_type=AuthProviderType.DINGTALK, name="DingTalk", config={})
    user = ExternalUser(external_id="same-id", name="Bob", unionid="same-id")

    with pytest.raises(ValueError, match="must not equal external_id"):
        adapter._validate_member_identifiers(provider, user)


def test_validate_member_identifiers_allows_wecom_without_unionid():
    adapter = _DummyAdapter()
    provider = SimpleNamespace(provider_type=AuthProviderType.WECOM, name="WeCom", config={})
    user = ExternalUser(external_id="zhangsan", name="Zhang San", unionid="")

    adapter._validate_member_identifiers(provider, user)


def test_sync_org_structure_skips_reconcile_after_member_failure(monkeypatch):
    adapter = _SyncAdapterWithFailure()
    db = SimpleNamespace(begin_nested=None, flush=None)

    @asynccontextmanager
    async def begin_nested():
        yield

    async def flush() -> None:
        return None

    async def fake_provider_update(*, db_obj, obj_in):
        if isinstance(getattr(db_obj, "config", None), dict) and isinstance(obj_in.get("config"), dict):
            db_obj.config = obj_in["config"]
        return db_obj

    monkeypatch.setattr(db, "begin_nested", begin_nested, raising=False)
    monkeypatch.setattr(db, "flush", flush, raising=False)
    from app.dao.identity_provider_dao import identity_provider_dao

    monkeypatch.setattr(identity_provider_dao, "update", fake_provider_update)

    result = asyncio.run(adapter.sync_org_structure(db))

    assert adapter.reconcile_called is False
    assert adapter.member_counts_updated is True
    assert "Reconcile skipped due to partial sync failures" in result["errors"]


def test_reconcile_marks_stale_members_and_departments(monkeypatch):
    adapter = _DummyAdapter()
    statements: list[str] = []

    class _Conn:
        async def execute(self, sql, params=None):
            statements.append(str(sql))

    class _Ctx:
        async def __aenter__(self):
            return _Conn()

        async def __aexit__(self, *_a):
            return False

    from app.services.org_sync import departments as dept_mod

    monkeypatch.setattr(dept_mod, "connection_ctx", lambda: _Ctx())

    asyncio.run(adapter._reconcile(None, uuid.uuid4(), datetime.now(UTC)))

    assert len(statements) == 2
    assert any("org_members" in s for s in statements)
    assert any("org_departments" in s for s in statements)
    assert all("deleted" in s for s in statements)


def test_google_workspace_adapter_parses_legacy_service_account_json_string():
    adapter = GoogleWorkspaceOrgSyncAdapter(
        config={
            "customer_id": "my_customer",
            "client_secret": '{"client_email":"svc@example.iam.gserviceaccount.com","private_key":"-----BEGIN PRIVATE KEY-----\\\\nabc\\\\n-----END PRIVATE KEY-----\\\\n"}',
            "delegated_admin_email": "admin@example.com",
        }
    )

    assert adapter.customer_id == "my_customer"
    assert adapter.delegated_admin_email == "admin@example.com"
    assert adapter.service_account["client_email"] == "svc@example.iam.gserviceaccount.com"


def test_google_workspace_adapter_uses_admin_authorization_email_as_primary_identity():
    adapter = GoogleWorkspaceOrgSyncAdapter(
        config={
            "client_id": "oauth-client-id.apps.googleusercontent.com",
            "client_secret": "oauth-client-secret",
            "google_admin_authorized_email": "admin@example.com",
        }
    )

    assert adapter.client_id == "oauth-client-id.apps.googleusercontent.com"
    assert adapter.client_secret == "oauth-client-secret"
    assert adapter.delegated_admin_email == "admin@example.com"
    assert adapter.service_account == {}


def test_google_workspace_adapter_registered():
    assert SYNC_ADAPTER_CLASSES["google_workspace"] is GoogleWorkspaceOrgSyncAdapter


def test_build_department_path_map_reconstructs_name_chain_from_internal_tree():
    root_id = uuid.uuid4()
    child_id = uuid.uuid4()
    leaf_id = uuid.uuid4()

    departments = [
        SimpleNamespace(id=leaf_id, external_id="leaf", name="平台组", parent_id=child_id),
        SimpleNamespace(id=child_id, external_id="child", name="研发部", parent_id=root_id),
        SimpleNamespace(id=root_id, external_id="root", name="总部", parent_id=None),
    ]

    path_map = build_department_path_map(departments)

    assert path_map[root_id] == "总部"
    assert path_map[child_id] == "总部/研发部"
    assert path_map[leaf_id] == "总部/研发部/平台组"


def test_build_department_path_map_treats_external_zero_root_as_empty_path():
    root_id = uuid.uuid4()
    child_id = uuid.uuid4()

    departments = [
        SimpleNamespace(id=child_id, external_id="200", name="研发部", parent_id=root_id),
        SimpleNamespace(id=root_id, external_id="0", name="Root", parent_id=None),
    ]

    path_map = build_department_path_map(departments)

    assert path_map[root_id] == ""
    assert path_map[child_id] == "研发部"
