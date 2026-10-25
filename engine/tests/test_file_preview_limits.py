import os
from unittest.mock import AsyncMock

import pytest

from app.api import file_previews
from app.services.storage_runtime import local
from app.services.storage_runtime.base import StorageReadLimitExceededError
from app.services.storage_runtime.fallback import FallbackStorageBackend
from app.services.storage_runtime.local import LocalStorageBackend
from file_preview_fixtures import preview_context

__all__ = ["preview_context"]


async def test_bytes_conflict_when_requested_version_is_stale(preview_context, monkeypatch):
    # Given
    ctx = preview_context
    reader = AsyncMock(side_effect=AssertionError("Stale requests must not read payload bytes"))
    original = ctx.storage.get_version_bounded
    version = await original(f"{ctx.agent_id}/report.pdf", file_previews.MAX_PREVIEW_BYTES)
    monkeypatch.setattr(ctx.storage, "get_version_bounded", AsyncMock(return_value=version))
    monkeypatch.setattr(ctx.storage, "read_bytes_bounded", reader)
    # When
    response = await ctx.client.get(
        f"{ctx.url}/preview-bytes", params={"path": "report.pdf", "version_token": "stale"}, headers=ctx.headers
    )
    # Then
    assert response.status_code == 409
    reader.assert_not_awaited()


@pytest.mark.parametrize("mutation", ["replace", "delete", "directory"])
async def test_bytes_conflict_when_file_changes_during_read(preview_context, monkeypatch, mutation):
    # Given
    ctx = preview_context
    key = f"{ctx.agent_id}/report.pdf"
    path = ctx.root / key
    version = await ctx.storage.get_version(key)
    original = ctx.storage.read_bytes_bounded
    reads = 0

    async def raced_read(key, maximum):
        nonlocal reads
        data = await original(key, maximum)
        reads += 1
        if reads == 2:
            match mutation:
                case "replace":
                    path.write_bytes(b"%PDF-mutated")
                case "delete":
                    path.unlink()
                case "directory":
                    path.unlink()
                    path.mkdir()
        return data

    monkeypatch.setattr(ctx.storage, "read_bytes_bounded", raced_read)
    # When
    response = await ctx.client.get(
        f"{ctx.url}/preview-bytes", params={"path": "report.pdf", "version_token": version.token}, headers=ctx.headers
    )
    # Then
    assert response.status_code == 409
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("endpoint", ["preview-metadata", "preview-bytes"])
async def test_preview_rejects_over_20_mib_before_hashing_or_reading(preview_context, monkeypatch, endpoint):
    # Given
    ctx = preview_context
    path = ctx.root / str(ctx.agent_id) / "report.pdf"
    with path.open("wb") as stream:
        stream.truncate(20 * 1024 * 1024 + 1)
    reader = AsyncMock(side_effect=AssertionError("Oversized local files must not be hashed or read"))
    monkeypatch.setattr(ctx.storage, "read_bytes_bounded", reader)
    monkeypatch.setattr(ctx.storage, "read_bytes", reader)
    # When
    response = await ctx.client.get(
        f"{ctx.url}/{endpoint}", params={"path": "report.pdf", "version_token": "stale"}, headers=ctx.headers
    )
    # Then
    assert response.status_code == 413
    assert response.headers["cache-control"] == "no-store"
    reader.assert_not_awaited()


async def test_bytes_reject_growth_during_read_at_actual_byte_limit(preview_context, monkeypatch):
    # Given
    ctx = preview_context
    key = f"{ctx.agent_id}/report.pdf"
    path = ctx.root / key
    path.write_bytes(b"12345678")
    version = await ctx.storage.get_version(key)
    monkeypatch.setattr(file_previews, "MAX_PREVIEW_BYTES", 8)
    original = local.read_bounded
    calls = 0
    observed = []

    def growing_read(stream, maximum):
        nonlocal calls
        calls += 1
        if calls == 2:
            with path.open("ab") as writer:
                writer.write(b"overflow")
        observed.append(stream)
        return original(stream, maximum)

    monkeypatch.setattr(local, "read_bounded", growing_read)
    # When
    response = await ctx.client.get(
        f"{ctx.url}/preview-bytes", params={"path": "report.pdf", "version_token": version.token}, headers=ctx.headers
    )
    # Then
    assert response.status_code == 413
    assert all(stream.closed for stream in observed)


async def test_bytes_allow_exact_limit_when_file_is_unchanged(preview_context, monkeypatch):
    # Given
    ctx = preview_context
    content = b"%PDF-preview"
    monkeypatch.setattr(file_previews, "MAX_PREVIEW_BYTES", len(content))
    version = await ctx.storage.get_version(f"{ctx.agent_id}/report.pdf")
    # When
    response = await ctx.client.get(
        f"{ctx.url}/preview-bytes", params={"path": "report.pdf", "version_token": version.token}, headers=ctx.headers
    )
    # Then
    assert response.status_code == 200
    assert response.content == content


async def test_preview_rejects_symlink_swap_when_it_occurs_after_path_validation(preview_context, monkeypatch):
    # Given
    ctx = preview_context
    path = ctx.root / str(ctx.agent_id) / "report.pdf"
    secret = ctx.root / "private.txt"
    secret.write_bytes(b"private")
    original = ctx.storage.read_bytes_bounded

    async def swapped_read(key, maximum):
        path.unlink()
        path.symlink_to(secret)
        return await original(key, maximum)

    monkeypatch.setattr(ctx.storage, "read_bytes_bounded", swapped_read)
    # When
    response = await ctx.client.get(f"{ctx.url}/preview-metadata", params={"path": "report.pdf"}, headers=ctx.headers)
    # Then
    assert response.status_code == 403
    assert b"private" not in response.content


async def test_preview_rejects_nonregular_file_without_opening_a_fifo(preview_context):
    # Given
    ctx = preview_context
    os.mkfifo(ctx.root / str(ctx.agent_id) / "pipe")
    # When
    response = await ctx.client.get(f"{ctx.url}/preview-metadata", params={"path": "pipe"}, headers=ctx.headers)
    # Then
    assert response.status_code == 404


async def test_fallback_metadata_uses_primary_identity_after_bounded_promotion(preview_context, monkeypatch):
    # Given
    ctx = preview_context
    primary = LocalStorageBackend(str(ctx.root / "primary"))
    fallback = FallbackStorageBackend(primary, ctx.storage)
    monkeypatch.setattr(file_previews, "get_storage_backend", lambda: fallback)
    monkeypatch.setattr(ctx.storage, "read_bytes", AsyncMock(side_effect=AssertionError("Unbounded legacy read")))
    # When
    response = await ctx.client.get(f"{ctx.url}/preview-metadata", params={"path": "report.pdf"}, headers=ctx.headers)
    # Then
    expected = await primary.get_version(f"{ctx.agent_id}/report.pdf")
    assert response.status_code == 200
    assert response.json()["version_token"] == expected.token
    assert response.json()["size"] == len(b"%PDF-preview")


async def test_preview_validation_errors_are_uncacheable(preview_context):
    # Given
    ctx = preview_context
    # When
    response = await ctx.client.get(f"{ctx.url}/preview-bytes", params={"path": "report.pdf"}, headers=ctx.headers)
    # Then
    assert response.status_code == 422
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"


async def test_fallback_version_rejects_oversize_legacy_before_promotion(tmp_path, monkeypatch):
    # Given
    primary = LocalStorageBackend(str(tmp_path / "primary"))
    legacy = LocalStorageBackend(str(tmp_path / "legacy"))
    await legacy.write_bytes("report.pdf", b"too large")
    storage = FallbackStorageBackend(primary, legacy)
    promoter = AsyncMock(side_effect=AssertionError("Oversized legacy file must not be promoted"))
    monkeypatch.setattr(primary, "create_bytes_if_absent", promoter)
    # When / Then
    with pytest.raises(StorageReadLimitExceededError):
        await storage.get_version_bounded("report.pdf", 8)
    promoter.assert_not_awaited()


async def test_fallback_version_uses_primary_winner_when_upload_races_promotion(tmp_path, monkeypatch):
    # Given
    primary = LocalStorageBackend(str(tmp_path / "primary"))
    legacy = LocalStorageBackend(str(tmp_path / "legacy"))
    await legacy.write_bytes("report.pdf", b"old")
    storage = FallbackStorageBackend(primary, legacy)
    original = legacy.read_bytes_bounded

    async def raced_read(key, maximum):
        data = await original(key, maximum)
        await primary.write_bytes(key, b"winning upload")
        return data

    monkeypatch.setattr(legacy, "read_bytes_bounded", raced_read)
    # When
    version = await storage.get_version_bounded("report.pdf", 20)
    # Then
    expected = await primary.get_version("report.pdf")
    assert version.token == expected.token
    assert version.size == len(b"winning upload")
