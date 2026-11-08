from threading import Event, get_ident

import anyio
import anyio.lowlevel
import pytest
from botocore.exceptions import ClientError, EndpointConnectionError
from botocore.stub import Stubber

from app.services.storage_runtime.base import StorageConditionalRaceError
from app.services.storage_runtime.s3 import S3StorageBackend
from s3_wire_server import S3Server


@pytest.fixture
def s3():
    backend = S3StorageBackend(bucket="bucket", prefix="tenant", access_key_id="key", secret_access_key="secret")
    client = backend._client_or_raise()
    with Stubber(client) as stubber:
        yield backend, stubber
        stubber.assert_no_pending_responses()


@pytest.mark.parametrize("content_type", [None, "text/plain"])
async def test_write_sends_complete_object_and_content_type(s3, content_type):
    backend, stubber = s3
    stubber.add_response(
        "put_object",
        {},
        {
            "Bucket": "bucket",
            "Key": "tenant/file",
            "Body": b"data",
            "ContentType": content_type or "application/octet-stream",
        },
    )
    await backend.write_bytes("file", b"data", content_type)


async def test_delete_sends_exact_object_key(s3):
    backend, stubber = s3
    stubber.add_response("delete_object", {}, {"Bucket": "bucket", "Key": "tenant/file"})
    await backend.delete("file")


@pytest.mark.parametrize("contents", [[], [{"Key": "tenant/dir/one"}, {"Key": "tenant/dir/two"}]])
async def test_delete_tree_deletes_listed_objects_or_nothing(s3, contents):
    backend, stubber = s3
    stubber.add_response("list_objects_v2", {"Contents": contents}, {"Bucket": "bucket", "Prefix": "tenant/dir/"})
    if contents:
        stubber.add_response("delete_objects", {}, {"Bucket": "bucket", "Delete": {"Objects": contents}})
    await backend.delete_tree("dir")


@pytest.mark.parametrize("operation", ["write_bytes", "delete", "delete_tree", "create_bytes_if_absent"])
async def test_mutation_errors_propagate(operation, monkeypatch):
    backend = S3StorageBackend(bucket="bucket")

    class OfflineClient:
        def list_objects_v2(self, **kwargs):
            return {"Contents": [{"Key": "dir/file"}]}

        def fail(self, **kwargs):
            raise EndpointConnectionError(endpoint_url="https://offline.invalid")

        put_object = fail
        delete_object = fail
        delete_objects = fail

    monkeypatch.setattr(backend, "_client_or_raise", OfflineClient)
    if operation in {"write_bytes", "create_bytes_if_absent"}:
        mutation = getattr(backend, operation)("dir", b"data")
    else:
        mutation = getattr(backend, operation)("dir")
    with pytest.raises(EndpointConnectionError):
        await mutation


@pytest.mark.parametrize("operation", ["write_bytes", "delete", "delete_tree", "create_bytes_if_absent"])
async def test_mutation_runs_off_loop_and_waits_for_worker_when_cancelled(operation, monkeypatch):
    backend = S3StorageBackend(bucket="bucket")
    entered, release = Event(), Event()
    worker_threads: list[int] = []
    completed = anyio.Event()
    loop_thread = get_ident()

    class BlockingClient:
        def list_objects_v2(self, **kwargs):
            return {"Contents": [{"Key": "dir/file"}]}

        def mutate(self, **kwargs):
            worker_threads.append(get_ident())
            entered.set()
            assert release.wait(10)
            return {}

        put_object = mutate
        delete_object = mutate
        delete_objects = mutate

    monkeypatch.setattr(backend, "_client_or_raise", BlockingClient)

    async def run_mutation():
        with anyio.CancelScope() as scope:
            scope_holder.append(scope)
            if operation in {"write_bytes", "create_bytes_if_absent"}:
                await getattr(backend, operation)("dir", b"data")
            else:
                await getattr(backend, operation)("dir")
            completed.set()

    scope_holder: list[anyio.CancelScope] = []
    async with anyio.create_task_group() as tasks:
        tasks.start_soon(run_mutation)
        try:
            assert await anyio.to_thread.run_sync(entered.wait, 10)
            assert len(worker_threads) == 1
            assert worker_threads[0] != loop_thread
            scope_holder[0].cancel()
            await anyio.lowlevel.checkpoint()
            assert not completed.is_set()
        finally:
            release.set()
    assert completed.is_set()


@pytest.mark.parametrize("statuses", [[409, 409, 200], [412], [409, 409, 409]])
async def test_file_create_streams_rewound_body_and_maps_conflicts(tmp_path, monkeypatch, statuses):
    path = tmp_path / "data"
    path.write_bytes(b"streamed content")
    backend = S3StorageBackend(bucket="bucket")
    bodies = []
    handles = []

    class Client:
        def put_object(self, **kwargs):
            body = kwargs["Body"]
            handles.append(body)
            assert kwargs["ContentLength"] == len(b"streamed content")
            assert kwargs["IfNoneMatch"] == "*"
            bodies.append(body.read())
            status = statuses[len(bodies) - 1]
            if status != 200:
                raise ClientError(
                    {"Error": {"Code": str(status)}, "ResponseMetadata": {"HTTPStatusCode": status}}, "PutObject"
                )
            return {}

    monkeypatch.setattr(backend, "_client_or_raise", Client)
    if statuses[-1] == 409:
        with pytest.raises(StorageConditionalRaceError):
            await backend.create_file_if_absent("file", path)
    else:
        result = await backend.create_file_if_absent("file", path)
        assert result.ok == (statuses[-1] == 200)
        assert result.conflict == (statuses[-1] == 412)
    assert bodies == [b"streamed content"] * len(statuses)
    assert all(body._stream.closed for body in handles)


async def test_file_create_sends_body_to_real_botocore_wire(tmp_path):
    path = tmp_path / "payload"
    path.write_bytes(b"wire payload")
    server = S3Server()
    server.start()
    try:
        backend = S3StorageBackend(
            bucket="bucket", endpoint_url=server.endpoint, access_key_id="key", secret_access_key="secret"
        )
        result = await backend.create_file_if_absent("file", path)
        assert result.ok
        assert server.state.objects["file"] == b"wire payload"
        assert (await backend.create_file_if_absent("file", path)).conflict
    finally:
        server.stop()
