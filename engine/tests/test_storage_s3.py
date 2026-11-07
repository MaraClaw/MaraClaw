import asyncio
from collections.abc import Mapping
from contextlib import aclosing
from io import BytesIO
from threading import Event, Lock, get_ident
from typing import TypedDict, Unpack, override

import anyio
import botocore.loaders
import pytest
from botocore.exceptions import ClientError, EndpointConnectionError
from botocore.stub import Stubber

from app.services.storage_runtime.base import (
    MAX_DIR_PAGE_LIMIT,
    AtomicCreateUnsupportedError,
    ConditionalWriteResult,
    StorageConditionalRaceError,
    StorageDeleteFailedError,
    StorageReadLimitExceededError,
    WriteCondition,
)
from app.services.storage_runtime.s3 import S3StorageBackend
from storage_walk_fixtures import EXPECTED_WALK, FILE_SIZE, PagedS3Client, s3_backend, shape, tree_client


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


@pytest.mark.parametrize("prefix", ["", "tenant"])
async def test_s3_walk_is_one_flat_listing_in_key_order_with_synthesized_directories(monkeypatch, prefix):
    # Given
    client = tree_client(prefix)
    backend = s3_backend(client, monkeypatch, prefix)
    # When
    entries = [entry async for entry in backend.walk("w")]
    # Then
    assert shape(entries) == list(EXPECTED_WALK)
    assert {entry.size for entry in entries if not entry.is_dir} == {FILE_SIZE}
    assert len(client.list_calls) == 1
    assert "Delimiter" not in client.list_calls[0]


async def test_s3_listing_and_walk_follow_every_page_past_one_thousand_entries(monkeypatch):
    # Given
    keys = {f"big/f{index:04d}.txt": 1 for index in range(2500)} | {"big/sub/x.txt": 1}
    client = PagedS3Client(keys)
    backend = s3_backend(client, monkeypatch)
    # When
    listed = await backend.list_dir("big")
    listing_calls = len(client.list_calls)
    client.list_calls.clear()
    walked = [entry async for entry in backend.walk("big")]
    # Then
    assert (len(listed), listed[0].name, listing_calls) == (2501, "sub", 3)
    assert [entry.name for entry in walked if entry.is_dir] == ["sub"]
    assert len([entry for entry in walked if not entry.is_dir]) == 2501
    assert len(client.list_calls) == 3


async def test_s3_walk_jumps_past_a_hidden_subtree_instead_of_paging_through_it(monkeypatch):
    # Given
    keys = {"w/a.txt": 1, "w/z.txt": 1} | {f"w/.cache/{index:04d}": 1 for index in range(3500)}
    client = PagedS3Client(keys)
    backend = s3_backend(client, monkeypatch)
    # When
    entries = [entry async for entry in backend.walk("w")]
    # Then
    assert [entry.key for entry in entries] == ["w/a.txt", "w/z.txt"]
    assert len(client.list_calls) == 2
    assert client.list_calls[1].get("StartAfter") == "w/.cache0"
    assert "ContinuationToken" not in client.list_calls[1]


@pytest.mark.parametrize("page_size", [1, 1000])
@pytest.mark.parametrize(
    ("hidden_keys", "expected"),
    [
        (
            ("w/visible/.env",),
            [("visible", "w/visible", True), ("z.txt", "w/z.txt", False)],
        ),
        (
            ("w/visible/.cache/secret",),
            [("visible", "w/visible", True), ("z.txt", "w/z.txt", False)],
        ),
        (
            ("w/a/b/.cache/deep/secret", "w/a/b/.env"),
            [("a", "w/a", True), ("b", "w/a/b", True), ("z.txt", "w/z.txt", False)],
        ),
        (
            ("w/v/.cache/secret", "w/v/b.txt"),
            [("v", "w/v", True), ("b.txt", "w/v/b.txt", False), ("z.txt", "w/z.txt", False)],
        ),
        (
            ("w/.top/secret", "w/v/.cache/secret", "w/v/w/.env", "w/v/w/x.txt"),
            [
                ("v", "w/v", True),
                ("w", "w/v/w", True),
                ("x.txt", "w/v/w/x.txt", False),
                ("z.txt", "w/z.txt", False),
            ],
        ),
    ],
)
async def test_s3_walk_yields_visible_ancestors_whose_only_descendants_are_hidden(
    monkeypatch: pytest.MonkeyPatch,
    hidden_keys: tuple[str, ...],
    expected: list[tuple[str, str, bool]],
    page_size: int,
):
    # Given
    keys = dict.fromkeys((*hidden_keys, "w/z.txt"), FILE_SIZE)
    backend = s3_backend(PagedS3Client(keys, page_size=page_size), monkeypatch)
    # When
    entries = [entry async for entry in backend.walk("w")]
    # Then
    assert shape(entries) == expected


async def test_s3_walk_yields_a_visible_ancestor_without_paging_its_hidden_subtree(monkeypatch):
    # Given
    keys = {"w/z.txt": 1} | {f"w/v/.cache/{index:04d}": 1 for index in range(3500)}
    client = PagedS3Client(keys)
    backend = s3_backend(client, monkeypatch)
    # When
    entries = [entry async for entry in backend.walk("w")]
    # Then
    assert shape(entries) == [("v", "w/v", True), ("z.txt", "w/z.txt", False)]
    assert len(client.list_calls) == 2
    assert client.list_calls[1].get("StartAfter") == "w/v/.cache0"


async def test_s3_walk_treats_trailing_slash_markers_as_directories_not_files(monkeypatch):
    # Given
    keys = dict.fromkeys(("w/", "w/docs/", "w/empty/", "w/.trash/"), 0) | {"w/docs/a.txt": 1}
    backend = s3_backend(PagedS3Client(keys), monkeypatch)
    # When
    entries = [entry async for entry in backend.walk("w")]
    # Then
    assert shape(entries) == [("docs", "w/docs", True), ("a.txt", "w/docs/a.txt", False), ("empty", "w/empty", True)]


async def test_s3_walk_stops_requesting_pages_once_the_consumer_closes_it(monkeypatch):
    # Given
    client = PagedS3Client({f"w/f{index:04d}": 1 for index in range(5000)})
    backend = s3_backend(client, monkeypatch)
    # When
    async with aclosing(backend.walk("w")) as walk:
        first = await anext(walk)
    # Then
    assert first.key == "w/f0000"
    assert len(client.list_calls) == 1


async def test_s3_walk_cancelled_mid_listing_requests_no_further_pages(monkeypatch):
    # Given
    second_page_started, release, second_page_done = Event(), Event(), Event()

    class StallingClient(PagedS3Client):
        @override
        def list_objects_v2(self, **kwargs: str | int) -> Mapping[str, object]:
            stalled = len(self.list_calls) == 1
            if stalled:
                second_page_started.set()
                assert release.wait(10)
            response = super().list_objects_v2(**kwargs)
            if stalled:
                second_page_done.set()
            return response

    client = StallingClient({f"w/f{index:04d}": 1 for index in range(5000)})
    backend = s3_backend(client, monkeypatch)

    async def drain() -> None:
        async for _entry in backend.walk("w"):
            pass

    task = asyncio.create_task(drain())
    # When
    assert await anyio.to_thread.run_sync(second_page_started.wait, 10)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    release.set()
    assert await anyio.to_thread.run_sync(second_page_done.wait, 10)
    # Then
    assert len(client.list_calls) == 2


async def test_s3_delete_tree_follows_every_listing_page(monkeypatch):
    # Given
    client = PagedS3Client({f"dir/f{index:04d}": 1 for index in range(2500)} | {"other/keep": 1})
    backend = s3_backend(client, monkeypatch)
    # When
    await backend.delete_tree("dir")
    # Then
    assert [len(call) for call in client.delete_calls] == [1000, 1000, 500]
    assert set(client.keys) == {"other/keep"}


async def test_s3_delete_tree_raises_when_the_provider_refuses_objects(monkeypatch):
    # Given
    client = PagedS3Client({"dir/a": 1, "dir/b": 1}, refuse_deletes=frozenset({"dir/b"}))
    backend = s3_backend(client, monkeypatch)
    # When
    with pytest.raises(StorageDeleteFailedError) as raised:
        await backend.delete_tree("dir")
    # Then
    assert raised.value.failed == 1
    assert set(client.keys) == {"dir/b"}


def page_shape(page) -> list[tuple[str, bool]]:
    return [(entry.name, entry.is_dir) for entry in page.entries]


@pytest.mark.parametrize("prefix", ["", "tenant"])
async def test_s3_page_sends_a_bounded_delimiter_listing_and_keeps_folders_in_key_order(monkeypatch, prefix):
    # Given
    client = tree_client(prefix)
    backend = s3_backend(client, monkeypatch, prefix)
    # When
    page = await backend.list_dir_page("w", limit=4)
    # Then
    assert page_shape(page) == [(".env", False), (".hidden", True), ("B.txt", False), ("a-b.txt", False)]
    assert page.has_more
    assert len(client.list_calls) == 1
    first = client.list_calls[0]
    assert (first["Prefix"], first["Delimiter"], first["MaxKeys"]) == (f"{prefix}/w/".lstrip("/"), "/", 5)
    assert "StartAfter" not in first
    assert page.entries[0].key == "w/.env"


async def test_s3_page_resumes_after_a_folder_without_repeating_it_or_losing_its_next_sibling(monkeypatch):
    # Given
    keys = dict.fromkeys(("w/a.txt", "w/a/inner.txt", "w/a/deep/more.txt", "w/a0", "w/b.txt"), 1)
    client = PagedS3Client(keys)
    backend = s3_backend(client, monkeypatch)
    # When
    first = await backend.list_dir_page("w", limit=2)
    resumed = await backend.list_dir_page("w", limit=2, after="a/")
    # Then
    assert page_shape(first) == [("a.txt", False), ("a", True)]
    assert first.has_more
    assert page_shape(resumed) == [("a0", False), ("b.txt", False)]
    assert not resumed.has_more
    start_after = str(client.list_calls[1]["StartAfter"])
    assert "w/a/inner.txt" < start_after < "w/a0"


async def test_s3_page_drops_a_folder_the_provider_returns_again_after_the_resume_marker(monkeypatch):
    # Given: a key sorting beyond the marker keeps the provider reporting the consumed folder
    keys = dict.fromkeys(("w/a/inner.txt", "w/a/\U0010ffffz", "w/b.txt"), 1)
    backend = s3_backend(PagedS3Client(keys), monkeypatch)
    # When
    page = await backend.list_dir_page("w", limit=5, after="a/")
    # Then
    assert page_shape(page) == [("b.txt", False)]


async def test_s3_page_resumes_after_a_file_with_a_start_after_of_that_key(monkeypatch):
    # Given
    client = PagedS3Client(dict.fromkeys(("w/a", "w/a/inner.txt", "w/b"), 1))
    backend = s3_backend(client, monkeypatch)
    # When
    page = await backend.list_dir_page("w", limit=5, after="a")
    # Then
    assert page_shape(page) == [("a", True), ("b", False)]
    assert client.list_calls[0]["StartAfter"] == "w/a"


async def test_s3_pages_walk_thousands_of_entries_one_bounded_request_per_page(monkeypatch):
    # Given
    client = PagedS3Client({f"big/f{index:04d}.txt": 1 for index in range(2500)} | {"big/sub/x.txt": 1})
    backend = s3_backend(client, monkeypatch)
    # When
    names: list[str] = []
    after = ""
    while True:
        page = await backend.list_dir_page("big", limit=200, after=after)
        assert len(page.entries) <= 200
        names.extend(entry.name for entry in page.entries)
        if not page.has_more:
            break
        after = f"{page.entries[-1].name}/" if page.entries[-1].is_dir else page.entries[-1].name
    # Then
    assert names == [*sorted(f"f{index:04d}.txt" for index in range(2500)), "sub"]
    assert len(client.list_calls) == 13
    assert {call["MaxKeys"] for call in client.list_calls} == {201}
    assert all("ContinuationToken" not in call for call in client.list_calls)


async def test_s3_page_continues_past_a_directory_marker_that_takes_a_slot(monkeypatch):
    # Given
    client = PagedS3Client(dict.fromkeys(("w/", "w/a", "w/b"), 1))
    backend = s3_backend(client, monkeypatch)
    # When
    page = await backend.list_dir_page("w", limit=1)
    # Then
    assert page_shape(page) == [("a", False)]
    assert page.has_more
    assert len(client.list_calls) == 2
    assert "ContinuationToken" in client.list_calls[1]
    assert "StartAfter" not in client.list_calls[1]


@pytest.mark.parametrize("limit", [0, -1, MAX_DIR_PAGE_LIMIT + 1])
async def test_s3_page_rejects_an_unbounded_limit_before_any_request(monkeypatch, limit):
    # Given
    client = PagedS3Client({"w/a": 1})
    backend = s3_backend(client, monkeypatch)
    # When / Then
    with pytest.raises(ValueError, match="page limit"):
        await backend.list_dir_page("w", limit=limit)
    assert client.list_calls == []
