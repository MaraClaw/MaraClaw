"""Platform-admin add/list/remove handlers for Linkup keys."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from app.api.admin_linkup import create_linkup_key, delete_linkup_key, list_linkup_keys
from app.api.admin_linkup import LinkupKeyCreateRequest
from app.records.linkup_api_key import LinkupApiKeyRecord
from app.services.linkup.keys import DuplicateLinkupKeyError


class MemoryKeyDao:
    def __init__(self) -> None:
        self.rows: dict[UUID, LinkupApiKeyRecord] = {}
        self.cursor: UUID | None = None

    async def get(self, key_id: UUID) -> LinkupApiKeyRecord | None:
        return self.rows.get(key_id)

    async def get_by_fingerprint(self, fingerprint: str) -> LinkupApiKeyRecord | None:
        for row in self.rows.values():
            if row.key_fingerprint == fingerprint:
                return row
        return None

    async def is_empty(self) -> bool:
        return not self.rows

    async def max_position(self) -> int | None:
        if not self.rows:
            return None
        return max(row.position for row in self.rows.values())

    async def list_ordered(self) -> list[LinkupApiKeyRecord]:
        return sorted(self.rows.values(), key=lambda row: row.position)

    async def list_active_ordered(self, *, now) -> list[LinkupApiKeyRecord]:
        del now
        return list(await self.list_ordered())

    async def create(self, *, obj_in: dict) -> LinkupApiKeyRecord:
        record = LinkupApiKeyRecord(
            id=uuid4(),
            label=str(obj_in["label"]),
            key_ciphertext=str(obj_in["key_ciphertext"]),
            key_fingerprint=str(obj_in["key_fingerprint"]),
            position=int(obj_in["position"]),
            status="active",
        )
        self.rows[record.id] = record
        return record

    async def delete(self, *, id: UUID) -> LinkupApiKeyRecord | None:
        return self.rows.pop(id, None)

    async def get_cursor_key_id(self) -> UUID | None:
        return self.cursor

    async def set_cursor_key_id(self, key_id: UUID | None) -> None:
        self.cursor = key_id


@pytest.fixture
def admin_user() -> SimpleNamespace:
    return SimpleNamespace(id=uuid4(), role="platform_admin", email="pa@example.test")


@pytest.fixture
def key_store(monkeypatch: pytest.MonkeyPatch) -> MemoryKeyDao:
    from app.services.linkup import keys as keys_mod

    store = MemoryKeyDao()
    monkeypatch.setattr(keys_mod, "linkup_api_key_dao", store)
    monkeypatch.setattr(keys_mod, "get_settings", lambda: SimpleNamespace(SECRET_KEY="test-secret", LINKUP_API_KEY=""))

    async def silent_audit(**_kwargs) -> None:
        return None

    monkeypatch.setattr("app.api.admin_linkup.write_admin_audit", silent_audit)
    return store


@pytest.mark.asyncio
async def test_admin_add_list_remove_never_returns_ciphertext(
    key_store: MemoryKeyDao, admin_user: SimpleNamespace
) -> None:
    created = await create_linkup_key(
        LinkupKeyCreateRequest(label="prod", api_key="lk-secret-1"),
        current_user=admin_user,
        client_ip="127.0.0.1",
    )
    assert created.label == "prod"
    assert "lk-secret-1" not in created.fingerprint
    assert created.model_dump().get("key_ciphertext") is None

    listed = await list_linkup_keys(current_user=admin_user)
    assert len(listed) == 1
    assert listed[0].id == created.id
    dumped = listed[0].model_dump()
    assert "key_ciphertext" not in dumped or dumped.get("key_ciphertext") is None
    assert "lk-secret-1" not in str(dumped)

    deleted = await delete_linkup_key(created.id, current_user=admin_user, client_ip="127.0.0.1")
    assert deleted.id == created.id
    assert await list_linkup_keys(current_user=admin_user) == []
    assert key_store.cursor is None


@pytest.mark.asyncio
async def test_admin_add_duplicate_returns_409(key_store: MemoryKeyDao, admin_user: SimpleNamespace) -> None:
    _ = key_store
    _ = await create_linkup_key(
        LinkupKeyCreateRequest(label="a", api_key="same"),
        current_user=admin_user,
        client_ip=None,
    )
    with pytest.raises(Exception) as exc:
        await create_linkup_key(
            LinkupKeyCreateRequest(label="b", api_key="same"),
            current_user=admin_user,
            client_ip=None,
        )
    status = getattr(exc.value, "status_code", None)
    assert status == 409


@pytest.mark.asyncio
async def test_admin_remove_missing_returns_404(key_store: MemoryKeyDao, admin_user: SimpleNamespace) -> None:
    _ = key_store
    with pytest.raises(Exception) as exc:
        await delete_linkup_key(uuid4(), current_user=admin_user, client_ip=None)
    assert getattr(exc.value, "status_code", None) == 404


def test_create_request_requires_label() -> None:
    with pytest.raises(ValidationError):
        LinkupKeyCreateRequest(api_key="lk-secret")
    with pytest.raises(ValidationError):
        LinkupKeyCreateRequest(label="", api_key="lk-secret")


def test_duplicate_error_type_is_public() -> None:
    assert issubclass(DuplicateLinkupKeyError, Exception)
