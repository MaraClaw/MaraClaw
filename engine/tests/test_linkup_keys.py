"""Add/remove and ring picker for stored Linkup API keys."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from app.core.security import encrypt_data
from app.records.linkup_api_key import LinkupApiKeyRecord
from app.services.linkup.errors import is_quota_error, is_transient_error
from app.services.linkup.keys import (
    DuplicateLinkupKeyError,
    LinkupKeyNotFoundError,
    add_key,
    advance_cursor,
    current_key,
    fingerprint_api_key,
    list_keys,
    public_key_view,
    remove_key,
)


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
        return sorted(self.rows.values(), key=lambda row: (row.position, str(row.id)))

    async def list_active_ordered(self, *, now: datetime) -> list[LinkupApiKeyRecord]:
        active = [
            row
            for row in self.rows.values()
            if row.status == "active" and (row.exhausted_until is None or row.exhausted_until <= now)
        ]
        return sorted(active, key=lambda row: (row.position, str(row.id)))

    async def create(self, *, obj_in: dict) -> LinkupApiKeyRecord:
        record = LinkupApiKeyRecord(
            id=uuid4(),
            label=str(obj_in["label"]),
            key_ciphertext=str(obj_in["key_ciphertext"]),
            key_fingerprint=str(obj_in["key_fingerprint"]),
            position=int(obj_in["position"]),
            status=str(obj_in.get("status") or "active"),
        )
        self.rows[record.id] = record
        return record

    async def update(self, *, db_obj: LinkupApiKeyRecord, obj_in: dict) -> LinkupApiKeyRecord:
        for key, value in obj_in.items():
            setattr(db_obj, key, value)
        return db_obj

    async def delete(self, *, id: UUID) -> LinkupApiKeyRecord | None:
        return self.rows.pop(id, None)

    async def get_cursor_key_id(self) -> UUID | None:
        return self.cursor

    async def set_cursor_key_id(self, key_id: UUID | None) -> None:
        self.cursor = key_id


@pytest.fixture
def key_dao(monkeypatch: pytest.MonkeyPatch) -> MemoryKeyDao:
    from app.services.linkup import keys as keys_mod

    store = MemoryKeyDao()
    monkeypatch.setattr(keys_mod, "linkup_api_key_dao", store)
    monkeypatch.setattr(keys_mod, "get_settings", lambda: SimpleNamespace(SECRET_KEY="test-secret", LINKUP_API_KEY=""))
    return store


@pytest.mark.asyncio
async def test_add_two_keys_and_remove_current_moves_cursor(key_dao: MemoryKeyDao) -> None:
    first = await add_key(label="one", api_key="lk-one")
    second = await add_key(label="two", api_key="lk-two")
    assert first.position == 0
    assert second.position == 1
    assert key_dao.cursor == first.id
    assert [row.label for row in await list_keys()] == ["one", "two"]

    await remove_key(first.id)
    assert key_dao.cursor == second.id
    assert [row.label for row in await list_keys()] == ["two"]


@pytest.mark.asyncio
async def test_remove_last_key_clears_cursor(key_dao: MemoryKeyDao) -> None:
    only = await add_key(label="only", api_key="lk-only")
    await remove_key(only.id)
    assert key_dao.cursor is None
    assert await list_keys() == []
    assert await current_key() is None


@pytest.mark.asyncio
async def test_add_rejects_blank_label(key_dao: MemoryKeyDao) -> None:
    _ = key_dao
    with pytest.raises(ValueError, match="label is required"):
        await add_key(label="   ", api_key="lk-secret")


@pytest.mark.asyncio
async def test_add_duplicate_fingerprint_raises(key_dao: MemoryKeyDao) -> None:
    _ = await add_key(label="a", api_key="same-secret")
    with pytest.raises(DuplicateLinkupKeyError):
        _ = await add_key(label="b", api_key="same-secret")


@pytest.mark.asyncio
async def test_remove_missing_key_raises(key_dao: MemoryKeyDao) -> None:
    with pytest.raises(LinkupKeyNotFoundError):
        await remove_key(uuid4())


@pytest.mark.asyncio
async def test_picker_cycles_three_keys_and_wraps(key_dao: MemoryKeyDao) -> None:
    a = await add_key(label="a", api_key="ka")
    b = await add_key(label="b", api_key="kb")
    c = await add_key(label="c", api_key="kc")
    assert (await current_key()).id == a.id
    nxt = await advance_cursor(a.id)
    assert nxt is not None and nxt.id == b.id
    nxt = await advance_cursor(b.id)
    assert nxt is not None and nxt.id == c.id
    nxt = await advance_cursor(c.id)
    assert nxt is not None and nxt.id == a.id


@pytest.mark.asyncio
async def test_picker_skips_exhausted_and_disabled(key_dao: MemoryKeyDao) -> None:
    a = await add_key(label="a", api_key="ka")
    b = await add_key(label="b", api_key="kb")
    c = await add_key(label="c", api_key="kc")
    b.status = "disabled"
    a.exhausted_until = datetime.now(UTC) + timedelta(hours=1)
    current = await current_key()
    assert current is not None and current.id == c.id


@pytest.mark.asyncio
async def test_public_view_hides_ciphertext(key_dao: MemoryKeyDao) -> None:
    record = await add_key(label="shown", api_key="super-secret-key")
    view = public_key_view(record)
    assert "key_ciphertext" not in view
    assert view["fingerprint"] == fingerprint_api_key("super-secret-key")
    assert encrypt_data("super-secret-key", "test-secret") not in str(view)


def test_quota_classifier_distinguishes_quota_from_transient() -> None:
    assert is_quota_error(402, "") is True
    assert is_quota_error(429, "insufficient quota for this plan") is True
    assert is_quota_error(429, "too many requests") is False
    assert is_transient_error(503) is True
    assert is_transient_error(429) is False
