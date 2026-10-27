import asyncio
from io import BytesIO
from threading import Event, Lock, get_ident
from typing import TypedDict, Unpack, override

import anyio
import botocore.loaders
import pytest
from botocore.exceptions import ClientError, EndpointConnectionError
from botocore.stub import Stubber

from app.services.storage_runtime.base import (
    AtomicCreateUnsupportedError,
    ConditionalWriteResult,
    StorageConditionalRaceError,
    StorageReadLimitExceededError,
    WriteCondition,
)
from app.services.storage_runtime.s3 import S3StorageBackend


class BotocoreConfigOptions(TypedDict):
    max_pool_connections: int
    proxies: dict[str, str]
    s3: dict[str, str]
    signature_version: str
    connect_timeout: int
    read_timeout: int
    tcp_keepalive: bool
    region_name: str | None


def test_s3_backend_passes_max_pool_connections(monkeypatch):
    config_instances: list[FakeConfig] = []
    client_calls: list[FakeConfig] = []

    class FakeConfig:
        def __init__(self, **kwargs: Unpack[BotocoreConfigOptions]) -> None:
            self.kwargs = kwargs
            config_instances.append(self)

    class FakeS3Client:
        pass

    class FakeSession:
        def client(
            self,
            service_name: str,
            *,
            endpoint_url: str | None,
            aws_access_key_id: str | None,
            aws_secret_access_key: str | None,
            config: FakeConfig,
        ) -> FakeS3Client:
            assert service_name == "s3"
            assert endpoint_url == "http://minio:9000"
            assert aws_access_key_id == "key"
            assert aws_secret_access_key == "secret"
            client_calls.append(config)
            return FakeS3Client()

    class FakeBoto3:
        Session = FakeSession

    class FakeBotocoreConfigModule:
        Config = FakeConfig

    fake_boto3 = FakeBoto3()

    import builtins

    real_import = builtins.__import__

    def fake_import(name, global_namespace=None, local_namespace=None, fromlist=(), level=0):
        if name == "boto3":
            return fake_boto3
        if name == "botocore.config":
            return FakeBotocoreConfigModule()
        return real_import(name, global_namespace, local_namespace, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    backend = S3StorageBackend(
        bucket="bucket",
        endpoint_url="http://minio:9000",
        access_key_id="key",
        secret_access_key="secret",
        max_pool_connections=64,
    )

    backend._client_or_raise()

    assert len(config_instances) == 1
    assert config_instances[0].kwargs["max_pool_connections"] == 64
    assert len(client_calls) == 1
    assert client_calls[0] is config_instances[0]


async def test_s3_absence_condition_has_one_winner_when_writers_race(monkeypatch):
    backend = S3StorageBackend(bucket="bucket")
    objects: dict[str, bytes] = {}
    lock = Lock()
    results: list[ConditionalWriteResult] = []

    class ConditionalClient:
        def put_object(self, **kwargs):
            with lock:
                if kwargs.get("IfNoneMatch") == "*" and kwargs["Key"] in objects:
                    raise ClientError(
                        {"Error": {"Code": "PreconditionFailed"}, "ResponseMetadata": {"HTTPStatusCode": 412}},
                        "PutObject",
                    )
                objects[kwargs["Key"]] = kwargs["Body"]
            return {"ETag": '"etag"'}

    monkeypatch.setattr(backend, "_client_or_raise", ConditionalClient)

    async def create(data: bytes) -> None:
        results.append(
            await backend.write_bytes_if_match("report.txt", data, condition=WriteCondition(require_absent=True))
        )

    async with anyio.create_task_group() as tasks:
        tasks.start_soon(create, b"first")
        tasks.start_soon(create, b"second")

    assert sum(result.ok for result in results) == 1
    assert sum(result.conflict for result in results) == 1


@pytest.fixture
def conditional_s3():
    backend = S3StorageBackend(bucket="bucket", prefix="tenant", access_key_id="key", secret_access_key="secret")
    client = backend._client_or_raise()
    with Stubber(client) as stubber:
        yield backend, stubber
        stubber.assert_no_pending_responses()


def conditional_params(content_type: str = "text/plain") -> dict[str, object]:
    return {
        "Bucket": "bucket",
        "Key": "tenant/report.txt",
        "Body": b"complete",
        "ContentType": content_type,
        "IfNoneMatch": "*",
    }


def test_s3_model_supports_native_creation_header():
    model = botocore.loaders.Loader().load_service_model("s3", "service-2")
    member = model["shapes"]["PutObjectRequest"]["members"]["IfNoneMatch"]
    assert member["location"] == "header"
    assert member["locationName"] == "If-None-Match"


@pytest.mark.parametrize("content_type", ["text/plain", None])
async def test_s3_create_sends_precondition_and_resolved_content_type(conditional_s3, content_type):
    backend, stubber = conditional_s3
    stubber.add_response(
        "put_object",
        {"ETag": '"created"', "VersionId": "version-1"},
        conditional_params(content_type or "application/octet-stream"),
    )

    result = await backend.create_bytes_if_absent("report.txt", b"complete", content_type=content_type)

    assert result.ok
    assert not result.conflict
    assert result.current_version.token == "version-1"


async def test_s3_create_reports_occupied_when_precondition_fails(conditional_s3):
    backend, stubber = conditional_s3
    stubber.add_client_error(
        "put_object",
        service_error_code="PreconditionFailed",
        http_status_code=412,
        expected_params=conditional_params(),
    )

    result = await backend.create_bytes_if_absent("report.txt", b"complete", "text/plain")

    assert result.conflict
    assert not result.ok


@pytest.mark.parametrize(("occupied_after_race", "code"), [(False, "409"), (True, "ConditionalRequestConflict")])
async def test_s3_create_retries_race_with_precondition(conditional_s3, occupied_after_race, code):
    backend, stubber = conditional_s3
    stubber.add_client_error(
        "put_object",
        service_error_code=code,
        http_status_code=409,
        expected_params=conditional_params(),
    )
    if occupied_after_race:
        stubber.add_client_error(
            "put_object",
            service_error_code="PreconditionFailed",
            http_status_code=412,
            expected_params=conditional_params(),
        )
    else:
        stubber.add_response("put_object", {"ETag": '"created"'}, conditional_params())

    result = await backend.create_bytes_if_absent("report.txt", b"complete", "text/plain")

    assert result.ok is not occupied_after_race
    assert result.conflict is occupied_after_race


async def test_s3_create_bounds_conditional_race_retries(conditional_s3):
    backend, stubber = conditional_s3
    for _attempt in range(3):
        stubber.add_client_error(
            "put_object",
            service_error_code="ConditionalRequestConflict",
            http_status_code=409,
            expected_params=conditional_params(),
        )

    with pytest.raises(StorageConditionalRaceError) as raised:
        await backend.create_bytes_if_absent("report.txt", b"complete", "text/plain")

    assert raised.value.attempts == 3


@pytest.mark.parametrize(
    ("code", "status", "exception"),
    [
        ("AccessDenied", 403, ClientError),
        ("InternalError", 500, ClientError),
        ("NotImplemented", 501, AtomicCreateUnsupportedError),
    ],
)
async def test_s3_create_preserves_provider_failure_classification(conditional_s3, code, status, exception):
    backend, stubber = conditional_s3
    stubber.add_client_error(
        "put_object", service_error_code=code, http_status_code=status, expected_params=conditional_params()
    )

    with pytest.raises(exception):
        await backend.create_bytes_if_absent("report.txt", b"complete", "text/plain")


async def test_gcs_atomic_create_fails_closed_but_ordinary_write_keeps_content_type(conditional_s3):
    backend, stubber = conditional_s3
    backend.endpoint_url = "https://storage.googleapis.com"
    with pytest.raises(AtomicCreateUnsupportedError):
        await backend.create_bytes_if_absent("report.txt", b"complete", "text/plain")
    params = conditional_params()
    del params["IfNoneMatch"]
    stubber.add_response("put_object", {}, params)

    await backend.write_bytes("report.txt", b"complete", "text/plain")


@pytest.mark.parametrize("code", ["404", "NoSuchKey", "NotFound", "AccessDenied", "NoSuchBucket"])
async def test_s3_version_only_treats_missing_objects_as_absent(code, monkeypatch):
    backend = S3StorageBackend(bucket="bucket")

    def fail_head(**kwargs):
        raise ClientError({"Error": {"Code": code}}, "HeadObject")

    class HeadClient:
        head_object = staticmethod(fail_head)

    monkeypatch.setattr(backend, "_client_or_raise", HeadClient)
    if code in {"AccessDenied", "NoSuchBucket"}:
        with pytest.raises(ClientError):
            await backend.get_version("report.txt")
    else:
        assert not (await backend.get_version("report.txt")).exists


@pytest.mark.parametrize("operation", ["get_version", "create_bytes_if_absent"])
async def test_s3_transport_errors_are_not_absence_or_conflict(operation, monkeypatch):
    backend = S3StorageBackend(bucket="bucket")

    class OfflineClient:
        def head_object(self, **kwargs):
            raise EndpointConnectionError(endpoint_url="https://offline.invalid")

        def put_object(self, **kwargs):
            raise EndpointConnectionError(endpoint_url="https://offline.invalid")

    monkeypatch.setattr(backend, "_client_or_raise", OfflineClient)

    if operation == "get_version":
        with pytest.raises(EndpointConnectionError):
            await backend.get_version("report.txt")
    else:
        with pytest.raises(EndpointConnectionError):
            await backend.create_bytes_if_absent("report.txt", b"complete")


class CountedBody(BytesIO):
    def __init__(self, data: bytes, chunk_size: int = 3):
        super().__init__(data)
        self.chunk_size = chunk_size
        self.consumed = 0
        self.requests: list[int] = []
        self.close_count = 0

    @override
    def read(self, amt: int | None = None) -> bytes:
        assert amt is not None
        assert amt > 0
        self.requests.append(amt)
        chunk = super().read(min(amt, self.chunk_size))
        self.consumed += len(chunk)
        return chunk

    @override
    def close(self) -> None:
        self.close_count += 1
        super().close()


@pytest.mark.parametrize(("limit", "size"), [(0, 0), (0, 1), (8, 0), (8, 7), (8, 8), (8, 9), (8, 200_000)])
async def test_s3_bounded_read_counts_stream_not_metadata_and_closes(monkeypatch, limit, size):
    backend = S3StorageBackend(bucket="bucket", prefix="tenant")
    body = CountedBody(b"x" * size)

    class Client:
        def get_object(self, **kwargs):
            assert kwargs == {"Bucket": "bucket", "Key": "tenant/report.bin"}
            return {"Body": body, "ContentLength": 1}

    monkeypatch.setattr(backend, "_client_or_raise", Client)

    if size > limit:
        with pytest.raises(StorageReadLimitExceededError) as raised:
            await backend.read_bytes_bounded("report.bin", limit)
        assert raised.value.max_bytes == limit
        assert raised.value.size_bytes == limit + 1
    else:
        assert await backend.read_bytes_bounded("report.bin", limit) == b"x" * size

    assert body.consumed == min(size, limit + 1)
    assert all(0 < size <= limit + 1 for size in body.requests)
    assert body.closed
    assert body.close_count == 1


async def test_s3_bounded_read_closes_body_on_read_error(monkeypatch):
    backend = S3StorageBackend(bucket="bucket")

    class FailedBody(CountedBody):
        @override
        def read(self, amt: int | None = None) -> bytes:
            if self.consumed:
                raise OSError("stream failed")
            return super().read(amt)

    body = FailedBody(b"document")

    class Client:
        def get_object(self, **kwargs):
            return {"Body": body}

    monkeypatch.setattr(backend, "_client_or_raise", Client)

    with pytest.raises(OSError, match="stream failed"):
        await backend.read_bytes_bounded("report.bin", 8)

    assert body.consumed == 3
    assert body.closed
    assert body.close_count == 1


@pytest.mark.parametrize("phase", ["get", "read"])
@pytest.mark.parametrize("outcome", ["success", "overflow", "error"])
async def test_s3_cancelled_awaiter_leaves_worker_owning_body_until_close(monkeypatch, phase, outcome):
    backend = S3StorageBackend(bucket="bucket")
    blocked, release, closed = Event(), Event(), Event()
    owners: list[int] = []

    def pause():
        blocked.set()
        assert release.wait(10)

    class BlockingBody(CountedBody):
        @override
        def read(self, amt: int | None = None) -> bytes:
            owners.append(get_ident())
            if phase == "read":
                pause()
            if outcome == "error":
                raise OSError("stream failed after cancellation")
            return super().read(amt)

        @override
        def close(self) -> None:
            owners.append(get_ident())
            super().close()
            closed.set()

    body = BlockingBody(b"ok" if outcome == "success" else b"x" * 200_000)

    class Client:
        def get_object(self, **kwargs):
            owners.append(get_ident())
            if phase == "get":
                pause()
            return {"Body": body}

    monkeypatch.setattr(backend, "_client_or_raise", Client)
    task = asyncio.create_task(backend.read_bytes_bounded("report.bin", 8))
    try:
        assert await anyio.to_thread.run_sync(blocked.wait, 10)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not body.closed
    finally:
        release.set()
        assert await anyio.to_thread.run_sync(closed.wait, 10)
        if not task.done():
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    assert body.close_count == 1
    assert body.consumed <= 9
    assert len(set(owners)) == 1


@pytest.mark.parametrize("code", ["404", "NoSuchKey", "NotFound", "AccessDenied", "NoSuchBucket"])
async def test_s3_bounded_read_preserves_provider_errors_except_missing_key(monkeypatch, code):
    backend = S3StorageBackend(bucket="bucket")

    class Client:
        def get_object(self, **kwargs):
            raise ClientError({"Error": {"Code": code}}, "GetObject")

    monkeypatch.setattr(backend, "_client_or_raise", Client)

    with pytest.raises(ClientError if code in {"AccessDenied", "NoSuchBucket"} else FileNotFoundError):
        await backend.read_bytes_bounded("report.bin", 8)
