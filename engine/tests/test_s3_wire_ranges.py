"""Ranged reads against the loopback S3 wire server: its own answers, the S3 backend, and the agent file tools.

The unit tests of ranged reads (``test_storage_ranges.py``) stub the SDK client. These send real botocore requests, so
the byte window, the ``Content-Range`` check and the ``416 InvalidRange`` of an empty or exhausted object are the wire
behaviour an S3 provider shows, and the tools are checked end to end against it.
"""

import hashlib
import http.client
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import override
from urllib.parse import urlsplit

import pytest
from botocore.exceptions import ClientError

from app.services import agent_tools
from app.services.agent_tool_exec import workspace_read, workspace_search_spill
from app.services.agent_tool_exec.registry import ToolArguments, ToolExecutionContext, resolve, use_execution_context
from app.services.agent_tool_exec.workspace_search_cursor import FILE_BYTES
from app.services.storage_runtime.base import StorageBackend
from app.services.storage_runtime.local import LocalStorageBackend
from app.services.storage_runtime.s3 import S3StorageBackend
from s3_wire_server import S3Server, S3State

NOTES = "workspace/notes.md"
OBJECT = b"0123456789"


@pytest.fixture(scope="module")
def server() -> Iterator[S3Server]:
    server = S3Server()
    server.start()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def wire(server: S3Server) -> S3Server:
    """The module's server with nothing stored and nothing recorded, so each test starts from an empty bucket."""
    server.state = S3State()
    return server


@pytest.fixture(autouse=True)
def _isolate_search_spill(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(workspace_search_spill, "_cache_path", lambda: tmp_path / "private-search" / "lines.sqlite3")


def s3_backend(server: S3Server) -> S3StorageBackend:
    return S3StorageBackend(
        bucket="bucket", endpoint_url=server.endpoint, access_key_id="key", secret_access_key="secret"
    )


def fetch(server: S3Server, key: str, byte_range: str | None = None) -> tuple[int, http.client.HTTPMessage, bytes]:
    connection = http.client.HTTPConnection(urlsplit(server.endpoint).netloc, timeout=10)
    try:
        connection.request("GET", f"/bucket/{key}", headers={} if byte_range is None else {"Range": byte_range})
        response = connection.getresponse()
        return response.status, response.headers, response.read()
    finally:
        connection.close()


@pytest.mark.parametrize(
    ("header", "content_range", "body"),
    [
        ("bytes=2-5", "bytes 2-5/10", b"2345"),
        ("bytes=0-0", "bytes 0-0/10", b"0"),
        ("bytes=9-9", "bytes 9-9/10", b"9"),
        ("bytes=4-", "bytes 4-9/10", b"456789"),
        ("bytes=-3", "bytes 7-9/10", b"789"),
        ("bytes=8-99", "bytes 8-9/10", b"89"),
        ("bytes=-99", "bytes 0-9/10", OBJECT),
    ],
)
def test_a_satisfiable_range_answers_206_with_the_span_it_selected(wire: S3Server, header, content_range, body):
    wire.state.objects["k"] = OBJECT

    status, headers, received = fetch(wire, "k", header)

    assert (status, headers["Content-Range"], received) == (206, content_range, body)
    assert headers["Content-Length"] == str(len(body))
    assert headers["ETag"] == f'"{hashlib.md5(OBJECT, usedforsecurity=False).hexdigest()}"'


@pytest.mark.parametrize("header", ["bytes=10-12", "bytes=10-", "bytes=99-100", "bytes=-0"])
@pytest.mark.parametrize(("size", "data"), [(10, OBJECT), (0, b"")])
def test_an_unsatisfiable_range_answers_416_invalid_range(wire: S3Server, header, size, data):
    wire.state.objects["k"] = data

    status, headers, body = fetch(wire, "k", header)

    assert (status, headers["Content-Range"]) == (416, f"bytes */{size}")
    assert b"<Code>InvalidRange</Code>" in body


@pytest.mark.parametrize("header", ["bytes=0-0", "bytes=0-", "bytes=-1"])
def test_any_range_of_an_empty_object_answers_416(wire: S3Server, header):
    wire.state.objects["empty"] = b""

    status, headers, body = fetch(wire, "empty", header)

    assert (status, headers["Content-Range"]) == (416, "bytes */0")
    assert b"<Code>InvalidRange</Code>" in body


@pytest.mark.parametrize("header", ["bytes=0-1,3-4", "bytes=5-2", "items=0-1", "bytes=abc", "bytes=-", "bytes=1-2-3"])
@pytest.mark.parametrize("data", [OBJECT, b""])
def test_a_header_that_is_not_one_range_serves_the_whole_object_with_200(wire: S3Server, header, data):
    wire.state.objects["k"] = data

    status, headers, body = fetch(wire, "k", header)

    assert (status, body) == (200, data)
    assert "Content-Range" not in headers


def test_a_ranged_get_of_a_missing_key_is_a_404_not_a_416(wire: S3Server):
    status, _, body = fetch(wire, "missing", "bytes=0-9")

    assert status == 404
    assert b"<Code>NoSuchKey</Code>" in body


def test_the_server_records_each_range_requested_for_a_key_in_order(wire: S3Server):
    wire.state.objects["k"] = OBJECT
    wire.state.objects["other"] = OBJECT

    _ = [fetch(wire, "k", header) for header in ("bytes=0-3", None, "bytes=9-", "bytes=0-1,3-4")]
    _ = fetch(wire, "other", "bytes=2-2")

    assert wire.state.ranges("k") == ["bytes=0-3", "bytes=9-", "bytes=0-1,3-4"]
    assert wire.state.ranges("other") == ["bytes=2-2"]
    assert wire.state.count("GET", "k") == 4


@pytest.mark.parametrize(
    ("offset", "limit", "expected"),
    [
        (250, 100, slice(250, 350)),
        (0, 7, slice(0, 7)),
        (1000, 100, slice(1000, 1024)),
        (1023, 100, slice(1023, 1024)),
        (1024, 100, slice(1024, 1024)),
        (5000, 100, slice(1024, 1024)),
    ],
    ids=["middle", "start", "clamped_at_the_end", "last_byte", "offset_is_the_size", "offset_past_the_size"],
)
async def test_the_s3_backend_reads_the_requested_window_and_nothing_past_the_end(
    wire: S3Server, offset: int, limit: int, expected: slice
):
    backend = s3_backend(wire)
    data = bytes(range(256)) * 4
    await backend.write_bytes("report.bin", data)

    assert await backend.read_bytes_bounded("report.bin", limit, offset=offset) == data[expected]

    assert wire.state.ranges("report.bin") == [f"bytes={offset}-{offset + limit - 1}"]
    assert wire.state.count("GET", "report.bin") == 1


async def test_the_s3_backend_reads_a_range_of_an_empty_object_as_no_bytes(wire: S3Server):
    backend = s3_backend(wire)
    await backend.write_bytes("empty.bin", b"")

    assert await backend.read_bytes_bounded("empty.bin", 8, offset=0) == b""

    assert wire.state.ranges("empty.bin") == ["bytes=0-7"]


async def test_botocore_raises_invalid_range_for_a_range_of_an_empty_object(wire: S3Server):
    backend = s3_backend(wire)
    await backend.write_bytes("empty.bin", b"")

    with pytest.raises(ClientError) as raised:
        _ = backend._client_or_raise().get_object(Bucket="bucket", Key="empty.bin", Range="bytes=0-7")

    assert raised.value.response["Error"]["Code"] == "InvalidRange"
    assert raised.value.response["ResponseMetadata"]["HTTPStatusCode"] == 416


async def test_the_s3_backend_reports_a_missing_object_for_a_ranged_read(wire: S3Server):
    with pytest.raises(FileNotFoundError):
        _ = await s3_backend(wire).read_bytes_bounded("missing.bin", 8, offset=0)


def lines(count: int, width: int = 100) -> bytes:
    """``count`` numbered lines of ``width`` bytes each, newline included."""
    return b"".join(f"{number:07} ".encode().ljust(width - 1, b"x") + b"\n" for number in range(1, count + 1))


async def stored(backend: StorageBackend, agent_id: uuid.UUID, data: bytes) -> str:
    key = f"{agent_id}/{NOTES}"
    await backend.write_bytes(key, data)
    return key


async def run_tool(
    monkeypatch: pytest.MonkeyPatch, backend: StorageBackend, name: str, agent_id: uuid.UUID, arguments: ToolArguments
) -> str:
    monkeypatch.setattr(agent_tools, "get_storage_backend", lambda: backend)
    handler = resolve(name)
    assert handler is not None
    with use_execution_context(ToolExecutionContext(tenant_id="tenant-a", workspace_root=agent_tools.WORKSPACE_ROOT)):
        result = handler(arguments=arguments, agent_id=agent_id, user_id=agent_id, session_id=name, on_output=None)
        return result if isinstance(result, str) else await result


class RecordedWindows(LocalStorageBackend):
    """The local backend, recording each ranged read as the ``Range`` header an object store would be sent."""

    def __init__(self, root: str):
        super().__init__(root)
        self.ranges: list[str] = []

    @override
    async def read_bytes_bounded(self, key: str, max_bytes: int, *, offset: int | None = None) -> bytes:
        assert offset is not None, "a tool read the whole object"
        self.ranges.append(f"bytes={offset}-{offset + max_bytes - 1}")
        return await super().read_bytes_bounded(key, max_bytes, offset=offset)


@pytest.mark.parametrize(
    "arguments",
    [{"offset": 31_464, "limit": 5}, {"offset": 20_000, "limit": 2}, {"offset": 10**6}, {"limit": 3000}],
    ids=["tail_to_the_end", "deep_window_that_stops_early", "offset_past_the_end", "first_page_over_two_reads"],
)
async def test_read_file_of_a_multi_mebibyte_file_matches_the_local_backend_in_ranged_reads_only(
    wire: S3Server, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, arguments: ToolArguments
):
    agent_id = uuid.uuid4()
    data = lines(31_467) + b"last line without a newline"
    local = RecordedWindows(str(tmp_path / "local"))
    remote = s3_backend(wire)
    _ = await stored(local, agent_id, data)
    key = await stored(remote, agent_id, data)

    expected = await run_tool(monkeypatch, local, "read_file", agent_id, {"path": NOTES, **arguments})
    result = await run_tool(monkeypatch, remote, "read_file", agent_id, {"path": NOTES, **arguments})

    assert result == expected
    assert len(data) > 3 * 1024 * 1024
    assert wire.state.ranges(key) == local.ranges
    assert wire.state.count("GET", key) == len(local.ranges)
    assert len(local.ranges) > 1
    assert local.ranges[0] == f"bytes=0-{workspace_read.READ_CHUNK_BYTES - 1}"


@pytest.mark.parametrize(
    "data",
    [b"", b"x" * (workspace_read.READ_CHUNK_BYTES - 1) + b"\n"],
    ids=["empty_file", "file_the_size_of_the_first_read"],
)
async def test_read_file_ends_a_file_at_the_providers_invalid_range_answer(
    wire: S3Server, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, data: bytes
):
    agent_id = uuid.uuid4()
    local = RecordedWindows(str(tmp_path / "local"))
    remote = s3_backend(wire)
    _ = await stored(local, agent_id, data)
    key = await stored(remote, agent_id, data)

    expected = await run_tool(monkeypatch, local, "read_file", agent_id, {"path": NOTES})
    result = await run_tool(monkeypatch, remote, "read_file", agent_id, {"path": NOTES})

    assert result == expected
    assert wire.state.ranges(key) == local.ranges
    assert wire.state.count("GET", key) == len(local.ranges)


@pytest.mark.parametrize("size", [2 * FILE_BYTES, 2 * FILE_BYTES + 777], ids=["window_multiple", "partial_last_window"])
async def test_search_files_finds_a_match_in_the_last_window_of_a_file_larger_than_one_window(
    wire: S3Server, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, size: int
):
    agent_id = uuid.uuid4()
    count = size // 100
    needle = b"needle-in-the-last-window".ljust(100 + size % 100 - 1, b"x") + b"\n"
    data = lines(count - 1) + needle
    assert len(data) == size
    local = RecordedWindows(str(tmp_path / "local"))
    remote = s3_backend(wire)
    _ = await stored(local, agent_id, data)
    key = await stored(remote, agent_id, data)
    arguments: ToolArguments = {"pattern": "needle-in-the-last-window", "path": "workspace"}

    expected = await run_tool(monkeypatch, local, "search_files", agent_id, arguments)
    result = await run_tool(monkeypatch, remote, "search_files", agent_id, arguments)

    assert result == expected
    assert f"{NOTES}:{count}: needle-in-the-last-window" in result
    assert wire.state.ranges(key) == local.ranges
    assert wire.state.count("GET", key) == len(local.ranges)
    assert len(local.ranges) > 1
