import hashlib

import pytest

from app.services.storage_runtime.base import (
    ConditionalWriteResult,
    StorageConditionalRaceError,
    StorageReadLimitExceededError,
)
from app.services.storage_runtime.fallback import FallbackStorageBackend
from app.services.storage_runtime.local import LocalStorageBackend
from app.services.storage_runtime.quota_errors import (
    StorageAccountingUnavailableError,
    StorageFileTooLargeError,
    StorageKeyPendingError,
    StorageQuotaExceededError,
)
from app.services.storage_runtime.quota_s3 import ManagedS3StorageBackend


@pytest.mark.parametrize("winner_size", [3, 20])
@pytest.mark.parametrize("timing", ["present", "promotion"])
async def test_fallback_snapshot_returns_bounded_primary_winner(tmp_path, monkeypatch, winner_size, timing):
    primary = LocalStorageBackend(str(tmp_path / "primary"))
    legacy = LocalStorageBackend(str(tmp_path / "legacy"))
    await legacy.write_bytes("report", b"old")
    create = primary.create_bytes_if_absent
    attempts = []

    async def competing_create(key, data, content_type=None):
        attempts.append(data)
        await primary.write_bytes(key, b"w" * winner_size)
        return await create(key, data, content_type)

    if timing == "present":
        await primary.write_bytes("report", b"w" * winner_size)
    monkeypatch.setattr(primary, "create_bytes_if_absent", competing_create)
    backend = FallbackStorageBackend(primary, legacy)

    if winner_size > 8:
        with pytest.raises(StorageReadLimitExceededError):
            await backend.read_snapshot_bounded("report", 8)
    else:
        snapshot = await backend.read_snapshot_bounded("report", 8)
        assert snapshot.data == b"w" * winner_size
        assert snapshot.sha256 == hashlib.sha256(b"w" * winner_size).hexdigest()
        assert snapshot.version == await primary.get_version("report")

    assert attempts == ([] if timing == "present" else [b"old"])
    assert (tmp_path / "legacy/report").read_bytes() == b"old"


@pytest.mark.parametrize("legacy_failure", ["missing", "overflow"])
@pytest.mark.parametrize("winner", [False, True])
async def test_fallback_snapshot_rechecks_primary_on_legacy_failure(tmp_path, monkeypatch, legacy_failure, winner):
    primary = LocalStorageBackend(str(tmp_path / "primary"))
    legacy = LocalStorageBackend(str(tmp_path / "legacy"))
    if legacy_failure == "overflow":
        await legacy.write_bytes("report", b"x" * 20)
    read = legacy.read_snapshot_bounded

    async def raced_read(key, cap):
        try:
            return await read(key, cap)
        finally:
            if winner:
                await primary.write_bytes(key, b"winner")

    monkeypatch.setattr(legacy, "read_snapshot_bounded", raced_read)
    backend = FallbackStorageBackend(primary, legacy)

    if winner:
        snapshot = await backend.read_snapshot_bounded("report", 8)
        assert snapshot.data == b"winner"
        assert snapshot.version == await primary.get_version("report")
    else:
        with pytest.raises(FileNotFoundError if legacy_failure == "missing" else StorageReadLimitExceededError):
            await backend.read_snapshot_bounded("report", 8)
        assert not (tmp_path / "primary").exists()


async def test_fallback_snapshot_stops_after_three_disappearing_promotions(tmp_path, monkeypatch):
    primary = LocalStorageBackend(str(tmp_path / "primary"))
    legacy = LocalStorageBackend(str(tmp_path / "legacy"))
    await legacy.write_bytes("report", b"old")
    attempts = []

    async def disappearing_create(key, data, content_type=None):
        attempts.append((key, data))
        return ConditionalWriteResult(ok=False, conflict=True)

    monkeypatch.setattr(primary, "create_bytes_if_absent", disappearing_create)

    with pytest.raises(StorageConditionalRaceError) as raised:
        await FallbackStorageBackend(primary, legacy).read_snapshot_bounded("report", 8)

    assert raised.value.attempts == 3
    assert attempts == [("report", b"old")] * 3


@pytest.mark.parametrize(
    "refusal",
    [
        StorageFileTooLargeError(1, 3),
        StorageQuotaExceededError(10, 9, 3),
        StorageAccountingUnavailableError(),
        StorageKeyPendingError(),
    ],
)
async def test_fallback_snapshot_propagates_managed_quota_refusal_without_provider_put(tmp_path, monkeypatch, refusal):
    primary = ManagedS3StorageBackend(bucket="bucket")
    legacy = LocalStorageBackend(str(tmp_path))
    key = "00000000-0000-0000-0000-000000000001/report"
    await legacy.write_bytes(key, b"old")
    admitted = []

    class MissingClient:
        def get_object(self, **kwargs):
            from botocore.exceptions import ClientError

            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")

        def put_object(self, **kwargs):
            raise AssertionError("quota refusal must occur before provider mutation")

    async def refuse(key, managed, data):
        admitted.append((key, data))
        raise refusal

    monkeypatch.setattr(primary, "_client_or_raise", MissingClient)
    monkeypatch.setattr(primary, "_admit_write", refuse)

    with pytest.raises(type(refusal)) as raised:
        await FallbackStorageBackend(primary, legacy).read_snapshot_bounded(key, 8)

    assert raised.value is refusal
    assert admitted == [(key, b"old")]
    assert (tmp_path / key).read_bytes() == b"old"
