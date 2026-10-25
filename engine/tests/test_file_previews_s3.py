from collections.abc import Callable
from io import BytesIO
from typing import override

import pytest
from botocore.exceptions import ClientError

from app.api import file_previews
from app.services.storage_runtime.s3 import S3StorageBackend
from file_preview_fixtures import preview_context

__all__ = ["preview_context"]


class TrackedBody(BytesIO):
    def __init__(self, data: bytes, mutate: Callable[[], None] | None):
        super().__init__(data)
        self.consumed = 0
        self.mutate = mutate

    @override
    def read(self, size: int | None = -1):
        data = super().read(size)
        self.consumed += len(data)
        if self.mutate is not None:
            mutation, self.mutate = self.mutate, None
            mutation()
        return data


class PreviewS3:
    def __init__(self, key: str):
        self.key = key
        self.data = b"office-binary"
        self.version_id = "s3-version-one"
        self.etag = "etag-one"
        self.reported_size: int | None = None
        self.mutate: Callable[[], None] | None = None
        self.exists = True
        self.bodies: list[TrackedBody] = []

    def head_object(self, **kwargs):
        assert kwargs == {"Bucket": "attachments", "Key": self.key}
        if not self.exists:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "HeadObject")
        return {
            "ContentLength": len(self.data) if self.reported_size is None else self.reported_size,
            "VersionId": self.version_id,
            "ETag": f'"{self.etag}"',
        }

    def get_object(self, **kwargs):
        assert kwargs == {"Bucket": "attachments", "Key": self.key}
        if not self.exists:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        body = TrackedBody(self.data, self.mutate)
        self.bodies.append(body)
        return {"Body": body, "ContentLength": self.reported_size or len(self.data)}

    def list_objects_v2(self, **kwargs):
        raise AssertionError("Previews must not list S3")

    def generate_presigned_url(self, **kwargs):
        raise AssertionError("Previews must not redirect to presigned URLs")


@pytest.fixture
def preview_s3(preview_context, monkeypatch):
    ctx = preview_context
    storage = S3StorageBackend(bucket="attachments", prefix="tenant")
    client = PreviewS3(f"tenant/{ctx.agent_id}/report.docx")
    monkeypatch.setattr(storage, "_client_or_raise", lambda: client)
    monkeypatch.setattr(file_previews, "get_storage_backend", lambda: storage)
    return client


@pytest.mark.parametrize(("version_id", "expected"), [("s3-version-one", "s3-version-one"), ("", "etag-one")])
async def test_s3_metadata_uses_head_identity_without_reading_or_redirecting(
    preview_context, preview_s3, version_id, expected
):
    # Given
    ctx, client = preview_context, preview_s3
    client.version_id = version_id
    # When
    response = await ctx.client.get(f"{ctx.url}/preview-metadata", params={"path": "report.docx"}, headers=ctx.headers)
    # Then
    assert response.status_code == 200
    assert response.json() == {
        "path": "report.docx",
        "name": "report.docx",
        "size": len(client.data),
        "version_token": expected,
        "content_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    }
    assert not client.bodies
    assert "location" not in response.headers


async def test_s3_bytes_return_binary_and_close_body_without_redirecting(preview_context, preview_s3):
    # Given
    ctx, client = preview_context, preview_s3
    # When
    response = await ctx.client.get(
        f"{ctx.url}/preview-bytes",
        headers=ctx.headers,
        params={"path": "report.docx", "version_token": "s3-version-one"},
    )
    # Then
    assert response.status_code == 200
    assert response.content == b"office-binary"
    assert "location" not in response.headers
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert len(client.bodies) == 1
    assert client.bodies[0].closed


@pytest.mark.parametrize("versioned", [True, False])
async def test_s3_bytes_conflict_when_identity_changes_during_get(preview_context, preview_s3, versioned):
    # Given
    ctx, client = preview_context, preview_s3
    client.version_id = "s3-version-one" if versioned else ""
    token = "s3-version-one" if versioned else "etag-one"

    def mutate():
        client.version_id = "s3-version-two" if versioned else ""
        client.etag = "etag-two"

    client.mutate = mutate
    # When
    response = await ctx.client.get(
        f"{ctx.url}/preview-bytes",
        headers=ctx.headers,
        params={"path": "report.docx", "version_token": token},
    )
    # Then
    assert response.status_code == 409
    assert client.bodies[0].closed


async def test_s3_bytes_conflict_when_file_is_deleted_during_get(preview_context, preview_s3):
    # Given
    ctx, client = preview_context, preview_s3
    client.mutate = lambda: setattr(client, "exists", False)
    # When
    response = await ctx.client.get(
        f"{ctx.url}/preview-bytes",
        headers=ctx.headers,
        params={"path": "report.docx", "version_token": "s3-version-one"},
    )
    # Then
    assert response.status_code == 409
    assert client.bodies[0].closed


@pytest.mark.parametrize("endpoint", ["preview-metadata", "preview-bytes"])
async def test_s3_preview_rejects_oversize_head_before_get(preview_context, preview_s3, endpoint):
    # Given
    ctx, client = preview_context, preview_s3
    client.reported_size = 20 * 1024 * 1024 + 1
    # When
    response = await ctx.client.get(
        f"{ctx.url}/{endpoint}",
        headers=ctx.headers,
        params={"path": "report.docx", "version_token": "s3-version-one"},
    )
    # Then
    assert response.status_code == 413
    assert not client.bodies


async def test_s3_bytes_measure_actual_size_when_head_underreports(preview_context, preview_s3, monkeypatch):
    # Given
    ctx, client = preview_context, preview_s3
    client.reported_size = 8
    monkeypatch.setattr(file_previews, "MAX_PREVIEW_BYTES", 8)
    # When
    response = await ctx.client.get(
        f"{ctx.url}/preview-bytes",
        headers=ctx.headers,
        params={"path": "report.docx", "version_token": "s3-version-one"},
    )
    # Then
    assert response.status_code == 413
    assert client.bodies[0].consumed == 9
    assert client.bodies[0].closed


async def test_s3_bytes_conflict_when_get_length_disagrees_with_head(preview_context, preview_s3):
    # Given
    ctx, client = preview_context, preview_s3
    client.reported_size = len(client.data) + 1
    # When
    response = await ctx.client.get(
        f"{ctx.url}/preview-bytes",
        headers=ctx.headers,
        params={"path": "report.docx", "version_token": "s3-version-one"},
    )
    # Then
    assert response.status_code == 409
    assert client.bodies[0].closed


async def test_s3_metadata_returns_not_found_when_head_has_no_object(preview_context, preview_s3):
    # Given
    ctx, client = preview_context, preview_s3
    client.exists = False
    # When
    response = await ctx.client.get(f"{ctx.url}/preview-metadata", params={"path": "report.docx"}, headers=ctx.headers)
    # Then
    assert response.status_code == 404
    assert not client.bodies
