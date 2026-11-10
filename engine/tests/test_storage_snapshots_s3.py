import hashlib
from typing import override

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError

from app.services.storage_runtime.base import StorageReadLimitExceededError
from app.services.storage_runtime.s3 import S3StorageBackend
from test_storage_s3 import CountedBody


@pytest.mark.parametrize(
    ("version_id", "etag", "token"),
    [("v1", '"multipart-2"', "v1"), ("", '"multipart-2"', "multipart-2"), ("", "", ":8")],
)
async def test_s3_snapshot_uses_get_identity_with_separate_actual_sha256(monkeypatch, version_id, etag, token):
    body = CountedBody(b"document")
    backend = S3StorageBackend(bucket="bucket", prefix="tenant")
    gets = []

    class Client:
        def get_object(self, **kwargs):
            gets.append(kwargs)
            return {"Body": body, "ContentLength": 8, "ETag": etag, "VersionId": version_id}

        def head_object(self, **kwargs):
            raise AssertionError("snapshot identity must come from GET")

    monkeypatch.setattr(backend, "_client_or_raise", Client)

    snapshot = await backend.read_snapshot_bounded("report.bin", 8)

    assert snapshot.data == b"document"
    assert snapshot.sha256 == hashlib.sha256(b"document").hexdigest()
    assert snapshot.version.token == token
    assert snapshot.version.etag == snapshot.version.content_hash == etag.strip('"')
    assert snapshot.version.size == 8
    assert gets == [{"Bucket": "bucket", "Key": "tenant/report.bin"}]
    assert (body.consumed, body.close_count) == (8, 1)


@pytest.mark.parametrize(("limit", "size"), [(0, 0), (0, 1), (8, 7), (8, 8), (8, 9), (8, 200_000)])
async def test_s3_snapshot_actual_byte_bound_ignores_underreported_length(monkeypatch, limit, size):
    body = CountedBody(b"x" * size)
    backend = S3StorageBackend(bucket="bucket")

    class Client:
        def get_object(self, **kwargs):
            return {"Body": body, "ContentLength": 0, "ETag": '"opaque"'}

    monkeypatch.setattr(backend, "_client_or_raise", Client)

    if size > limit:
        with pytest.raises(StorageReadLimitExceededError) as raised:
            await backend.read_snapshot_bounded("report.bin", limit)
        assert raised.value.size_bytes == limit + 1
    else:
        snapshot = await backend.read_snapshot_bounded("report.bin", limit)
        assert (snapshot.data, snapshot.version.size) == (b"x" * size, size)

    assert (body.consumed, body.close_count) == (min(size, limit + 1), 1)


async def test_s3_snapshot_known_oversize_closes_without_consuming(monkeypatch):
    body = CountedBody(b"x" * 20)
    backend = S3StorageBackend(bucket="bucket")

    class Client:
        def get_object(self, **kwargs):
            return {"Body": body, "ContentLength": 20}

    monkeypatch.setattr(backend, "_client_or_raise", Client)

    with pytest.raises(StorageReadLimitExceededError) as raised:
        await backend.read_snapshot_bounded("report.bin", 8)

    assert raised.value.size_bytes == 20
    assert (body.consumed, body.close_count) == (0, 1)


@pytest.mark.parametrize("failure", ["missing", "denied", "transport", "read"])
async def test_s3_snapshot_preserves_provider_failures_and_read_cleanup(monkeypatch, failure):
    backend = S3StorageBackend(bucket="bucket")

    class FailedBody(CountedBody):
        @override
        def read(self, amt: int | None = None) -> bytes:
            raise OSError("stream failed")

    body = FailedBody(b"document")

    class Client:
        def get_object(self, **kwargs):
            if failure == "transport":
                raise EndpointConnectionError(endpoint_url="https://offline.invalid")
            if failure != "read":
                raise ClientError(
                    {"Error": {"Code": "NoSuchKey" if failure == "missing" else "AccessDenied"}}, "GetObject"
                )
            return {"Body": body}

    monkeypatch.setattr(backend, "_client_or_raise", Client)
    error = {
        "missing": FileNotFoundError,
        "denied": ClientError,
        "transport": EndpointConnectionError,
        "read": OSError,
    }[failure]

    with pytest.raises(error):
        await backend.read_snapshot_bounded("report.bin", 8)

    assert body.close_count == (1 if failure == "read" else 0)
