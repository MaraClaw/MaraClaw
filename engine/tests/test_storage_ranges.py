from collections.abc import Mapping
from io import BytesIO
from pathlib import Path
from threading import Event
from typing import Unpack, override

import anyio
import pytest
from botocore.exceptions import ClientError

from app.services.storage_runtime.base import StorageBackend
from app.services.storage_runtime.fallback import FallbackStorageBackend
from app.services.storage_runtime.local import LocalStorageBackend
from app.services.storage_runtime.s3 import S3StorageBackend
from app.services.storage_runtime.s3_connection import S3GetArguments


class _Body(BytesIO):
    def __init__(self, data: bytes):
        super().__init__(data)
        self.consumed: int = 0

    @override
    def read(self, size: int | None = None) -> bytes:
        assert size is not None
        assert 0 < size <= 65_536
        data = super().read(min(size, 3))
        self.consumed += len(data)
        return data


class _RangeClient:
    def __init__(self, data: bytes):
        self.data: bytes = data
        self.bodies: list[_Body] = []
        self.calls: list[S3GetArguments] = []

    def get_object(self, **arguments: Unpack[S3GetArguments]) -> Mapping[str, object]:
        self.calls.append(arguments)
        assert "Range" in arguments
        start, end = arguments["Range"].removeprefix("bytes=").split("-")
        offset, stop = int(start), int(end)
        if offset >= len(self.data):
            raise ClientError({"Error": {"Code": "InvalidRange"}}, "GetObject")
        body = _Body(self.data[offset : stop + 1])
        self.bodies.append(body)
        return {"Body": body, "ContentRange": f"bytes {offset}-{min(stop, len(self.data) - 1)}/{len(self.data)}"}


@pytest.mark.parametrize("kind", ["local", "s3", "fallback"])
@pytest.mark.parametrize(("offset", "limit"), [(0, 0), (0, 8), (3, 8), (10, 8), (11, 8), (20, 8)])
async def test_ranges_return_only_the_requested_bytes_without_promoting_partial_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str, offset: int, limit: int
):
    data = b"0123456789a"
    client = _RangeClient(data)
    primary = LocalStorageBackend(str(tmp_path / "primary"))
    backend: StorageBackend
    if kind == "s3":
        s3 = S3StorageBackend(bucket="bucket", prefix="tenant")
        monkeypatch.setattr(s3, "_client_or_raise", lambda: client)
        backend = s3
    else:
        legacy = LocalStorageBackend(str(tmp_path / "legacy"))
        await legacy.write_bytes("data.bin", data)
        backend = legacy if kind == "local" else FallbackStorageBackend(primary, legacy)

    result = await backend.read_bytes_bounded("data.bin", limit, offset=offset)

    assert result == data[offset : offset + limit]
    assert not (tmp_path / "primary").exists()
    assert all(body.closed and body.consumed <= limit for body in client.bodies)
    if kind == "s3" and limit:
        assert client.calls == [
            {"Bucket": "bucket", "Key": "tenant/data.bin", "Range": f"bytes={offset}-{offset + limit - 1}"}
        ]


@pytest.mark.parametrize("header", ["", "bytes 0-7/100"])
async def test_s3_range_rejects_ignored_or_wrong_offset_and_closes_the_body(monkeypatch, header):
    backend = S3StorageBackend(bucket="bucket")
    body = _Body(b"wrong bytes")

    class Client:
        def get_object(self, **_arguments):
            return {"Body": body, "ContentRange": header}

    monkeypatch.setattr(backend, "_client_or_raise", Client)

    with pytest.raises(OSError, match="did not honor"):
        await backend.read_bytes_bounded("data.bin", 8, offset=3)

    assert body.closed
    assert body.consumed == 0


@pytest.mark.parametrize("kind", ["local", "s3"])
async def test_range_rejects_negative_offset_without_io(tmp_path, monkeypatch, kind):
    backend = LocalStorageBackend(str(tmp_path)) if kind == "local" else S3StorageBackend(bucket="bucket")

    with pytest.raises(ValueError, match="nonnegative"):
        await backend.read_bytes_bounded("data.bin", 8, offset=-1)


async def test_cancelled_s3_range_keeps_body_owned_by_worker_until_it_closes(monkeypatch):
    backend = S3StorageBackend(bucket="bucket")
    started, release, closed = Event(), Event(), Event()
    ended = anyio.Event()
    scopes: list[anyio.CancelScope] = []

    class BlockingBody(_Body):
        @override
        def read(self, size: int | None = None) -> bytes:
            started.set()
            assert release.wait(5)
            return super().read(size)

        @override
        def close(self) -> None:
            super().close()
            closed.set()

    body = BlockingBody(b"abcdefghi")

    class Client:
        def get_object(self, **_arguments):
            return {"Body": body, "ContentRange": "bytes 3-10/100"}

    monkeypatch.setattr(backend, "_client_or_raise", Client)

    async def consume() -> None:
        try:
            with anyio.CancelScope() as scope:
                scopes.append(scope)
                await backend.read_bytes_bounded("data.bin", 8, offset=3)
        finally:
            ended.set()

    try:
        async with anyio.create_task_group() as group:
            group.start_soon(consume)
            assert await anyio.to_thread.run_sync(started.wait, 5)
            scopes[0].cancel()
            with anyio.fail_after(5):
                await ended.wait()
            assert not body.closed
    finally:
        release.set()
        assert await anyio.to_thread.run_sync(closed.wait, 5)

    assert body.consumed == 8
