"""An S3 brownout: reads ride out throttling on their own client, writes make one attempt, a full lane sheds."""

import asyncio
from collections.abc import Awaitable, Callable, Iterator
from types import SimpleNamespace

import botocore.endpoint
import pytest
from botocore.config import Config
from botocore.exceptions import ClientError
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.core.thread_slots import SlotsBusyError
from app.services.storage_runtime.base import WriteCondition
from app.services.storage_runtime.quota_s3 import ManagedS3StorageBackend
from app.services.storage_runtime.s3 import S3StorageBackend
from app.services.storage_runtime.s3_connection import LANE_WAITERS_PER_THREAD, READ_RETRIES
from s3_wire_server import Gate, S3Server, Throttle

READ_ATTEMPTS = READ_RETRIES["total_max_attempts"]


@pytest.fixture
def server() -> Iterator[S3Server]:
    wire = S3Server()
    wire.start()
    try:
        yield wire
    finally:
        wire.stop()


@pytest.fixture
def backoffs(monkeypatch: pytest.MonkeyPatch) -> list[int | float]:
    """The delays botocore would sleep between attempts; they are recorded instead of waited for."""
    delays: list[int | float] = []
    monkeypatch.setattr(botocore.endpoint, "time", SimpleNamespace(sleep=delays.append))
    return delays


def managed(server: S3Server, *, thread_lane_size: int = 32, max_pool_connections: int = 50) -> ManagedS3StorageBackend:
    """The production backend; keys outside an agent prefix pass straight through, so no ledger is needed."""
    return ManagedS3StorageBackend(
        bucket="bucket",
        endpoint_url=server.endpoint,
        access_key_id="key",
        secret_access_key="secret",
        thread_lane_size=thread_lane_size,
        max_pool_connections=max_pool_connections,
    )


async def names(backend: S3StorageBackend) -> list[str]:
    return [entry.name for entry in await backend.list_dir("dir")]


async def snapshot(backend: S3StorageBackend) -> bytes:
    return (await backend.read_snapshot_bounded("dir/a.txt", 16)).data


async def materialized(backend: S3StorageBackend) -> bytes:
    file = await backend.materialize("dir/a.txt")
    try:
        return file.path.read_bytes()
    finally:
        file.close()


type Read = Callable[[S3StorageBackend], Awaitable[object]]
READS: list[tuple[str, str, Read, object]] = [
    ("LIST", "dir/a.txt", lambda b: b.exists("dir/a.txt"), True),
    ("LIST", "dir/a.txt", lambda b: b.is_file("dir/a.txt"), True),
    ("LIST", "dir/", lambda b: b.is_dir("dir"), True),
    ("LIST", "dir/", names, ["a.txt"]),
    ("GET", "dir/a.txt", lambda b: b.read_bytes("dir/a.txt"), b"alpha"),
    ("GET", "dir/a.txt", lambda b: b.read_bytes_bounded("dir/a.txt", 16), b"alpha"),
    ("GET", "dir/a.txt", lambda b: b.read_bytes_bounded("dir/a.txt", 3, offset=1), b"lph"),
    ("GET", "dir/a.txt", snapshot, b"alpha"),
    ("GET", "dir/a.txt", materialized, b"alpha"),
]
READ_IDS = ["exists", "is_file", "is_dir", "list_dir", "read_bytes", "bounded", "ranged", "snapshot", "materialize"]


@pytest.mark.parametrize(("verb", "target", "read", "expected"), READS, ids=READ_IDS)
async def test_a_read_throttled_twice_succeeds_on_the_third_attempt(
    server: S3Server, backoffs: list[int | float], verb: str, target: str, read: Read, expected: object
):
    server.state.objects["dir/a.txt"] = b"alpha"
    server.state.throttles[(verb, target)] = Throttle(remaining=2)

    assert await read(managed(server)) == expected

    assert server.state.count(verb, target) == 3
    assert len(backoffs) == 2


async def test_a_read_that_stays_throttled_stops_at_its_attempt_budget(server: S3Server, backoffs: list[int | float]):
    server.state.objects["dir/a.txt"] = b"alpha"
    server.state.throttles[("GET", "dir/a.txt")] = Throttle(remaining=100)

    with pytest.raises(ClientError) as raised:
        _ = await managed(server).read_bytes("dir/a.txt")

    assert raised.value.response["Error"]["Code"] == "SlowDown"
    assert server.state.count("GET", "dir/a.txt") == READ_ATTEMPTS
    assert len(backoffs) == READ_ATTEMPTS - 1


async def test_a_missing_object_is_not_retried(server: S3Server, backoffs: list[int | float]):
    with pytest.raises(FileNotFoundError):
        _ = await managed(server).read_bytes("dir/gone.txt")

    assert server.state.count("GET", "dir/gone.txt") == 1
    assert backoffs == []


type Write = Callable[[ManagedS3StorageBackend], Awaitable[object]]
WRITES: list[tuple[str, Write]] = [
    ("PUT", lambda b: b.write_bytes("plain/a.txt", b"data", "text/plain")),
    ("PUT", lambda b: b.create_bytes_if_absent("plain/a.txt", b"data")),
    ("DELETE", lambda b: b.delete("plain/a.txt")),
]


@pytest.mark.parametrize(("verb", "write"), WRITES, ids=["write_bytes", "create_if_absent", "delete"])
async def test_a_throttled_write_fails_after_one_attempt(
    server: S3Server, backoffs: list[int | float], verb: str, write: Write
):
    server.state.objects["plain/a.txt"] = b"before"
    server.state.throttles[(verb, "plain/a.txt")] = Throttle()

    with pytest.raises(ClientError) as raised:
        _ = await write(managed(server))

    assert raised.value.response["Error"]["Code"] == "SlowDown"
    assert server.state.count(verb, "plain/a.txt") == 1
    assert server.state.objects["plain/a.txt"] == b"before"
    assert backoffs == []


async def test_get_version_is_never_retried_and_a_conditional_write_stops_before_its_put(
    server: S3Server, backoffs: list[int | float]
):
    server.state.objects["plain/a.txt"] = b"before"
    server.state.throttles[("HEAD", "plain/a.txt")] = Throttle(remaining=2)
    backend = managed(server)

    with pytest.raises(ClientError):
        _ = await backend.get_version("plain/a.txt")
    with pytest.raises(ClientError):
        _ = await backend.write_bytes_if_match("plain/a.txt", b"after", condition=WriteCondition(version_token="old"))

    assert server.state.count("HEAD", "plain/a.txt") == 2
    assert server.state.count("PUT", "plain/a.txt") == 0
    assert backoffs == []


async def test_environment_retry_settings_change_neither_clients_attempts(
    server: S3Server, backoffs: list[int | float], monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("AWS_MAX_ATTEMPTS", "9")
    server.state.objects["dir/a.txt"] = b"alpha"
    server.state.throttles[("PUT", "plain/b.txt")] = Throttle(remaining=100)
    server.state.throttles[("GET", "dir/a.txt")] = Throttle(remaining=100)
    backend = managed(server)

    with pytest.raises(ClientError):
        await backend.write_bytes("plain/b.txt", b"data")
    with pytest.raises(ClientError):
        _ = await backend.read_bytes("dir/a.txt")

    assert server.state.count("PUT", "plain/b.txt") == 1
    assert server.state.count("GET", "dir/a.txt") == READ_ATTEMPTS


def test_the_read_client_shares_the_mutation_clients_endpoint_and_pool_but_not_its_retries(server: S3Server):
    backend = managed(server, max_pool_connections=7)
    mutation, read = backend._boto_config(), backend._read_boto_config()

    def option(config: Config, name: str) -> object:
        return getattr(config, name)

    shared = ("max_pool_connections", "proxies", "s3", "signature_version", "connect_timeout", "read_timeout")
    assert [option(read, name) for name in shared] == [option(mutation, name) for name in shared]
    assert option(mutation, "max_pool_connections") == 7
    assert option(mutation, "retries") == {"total_max_attempts": 1, "mode": "standard"}
    assert option(read, "retries") == READ_RETRIES
    assert backend._client_or_raise() is not backend._read_client_or_raise()


async def saturate(server: S3Server, backend: ManagedS3StorageBackend) -> tuple[Gate, asyncio.Task[None]]:
    """One write held inside the provider on the only lane thread, plus a full queue of reads of its key."""
    gate = server.state.gates.setdefault("plain/held", Gate())
    held = asyncio.create_task(backend.write_bytes("plain/held", b"held"))
    assert await asyncio.to_thread(gate.arrived.wait, 10)
    return gate, held


async def test_a_saturated_lane_refuses_the_next_call_before_it_reaches_s3(server: S3Server):
    backend = managed(server, thread_lane_size=1)
    gate, held = await saturate(server, backend)
    queued = [asyncio.create_task(backend.read_bytes("plain/held")) for _ in range(LANE_WAITERS_PER_THREAD)]
    await asyncio.sleep(0)
    try:
        with pytest.raises(SlotsBusyError):
            _ = await backend.read_bytes("plain/held")
        with pytest.raises(SlotsBusyError):
            await backend.write_bytes("plain/other", b"never sent")
        assert server.state.count("GET", "plain/held") == 0
        assert server.state.count("PUT", "plain/other") == 0
    finally:
        gate.release.set()

    await asyncio.wait_for(held, 10)
    assert await asyncio.wait_for(asyncio.gather(*queued), 10) == [b"held"] * LANE_WAITERS_PER_THREAD
    assert await backend.read_bytes("plain/held") == b"held"


async def test_a_waiter_cancelled_in_a_full_queue_frees_its_place(server: S3Server):
    backend = managed(server, thread_lane_size=1)
    gate, held = await saturate(server, backend)
    queued = [asyncio.create_task(backend.read_bytes("plain/held")) for _ in range(LANE_WAITERS_PER_THREAD)]
    await asyncio.sleep(0)
    try:
        with pytest.raises(SlotsBusyError):
            _ = await backend.read_bytes("plain/held")
        queued[-1].cancel()
        with pytest.raises(asyncio.CancelledError):
            await queued[-1]
        replacement = asyncio.create_task(backend.read_bytes("plain/held"))
        await asyncio.sleep(0)
        assert not replacement.done()
    finally:
        gate.release.set()

    await asyncio.wait_for(held, 10)
    results = await asyncio.wait_for(asyncio.gather(*queued[:-1], replacement), 10)
    assert results == [b"held"] * LANE_WAITERS_PER_THREAD


async def test_a_route_over_a_saturated_lane_answers_503_with_retry_after_then_recovers(server: S3Server):
    from app.main import app as engine

    api = FastAPI()
    api.add_exception_handler(SlotsBusyError, engine.exception_handlers[SlotsBusyError])
    backend = managed(server, thread_lane_size=1)

    @api.get("/stored")
    async def stored() -> dict[str, str]:
        return {"data": (await backend.read_bytes("plain/held")).decode()}

    gate, held = await saturate(server, backend)
    queued = [asyncio.create_task(backend.read_bytes("plain/held")) for _ in range(LANE_WAITERS_PER_THREAD)]
    await asyncio.sleep(0)
    async with AsyncClient(transport=ASGITransport(app=api), base_url="http://engine") as client:
        try:
            refused = await client.get("/stored")
        finally:
            gate.release.set()
        await asyncio.wait_for(held, 10)
        _ = await asyncio.wait_for(asyncio.gather(*queued), 10)
        served = await client.get("/stored")

    assert refused.status_code == 503
    assert refused.headers["Retry-After"] == "5"
    assert refused.json()["detail"]["code"] == "storage_busy"
    assert server.state.count("GET", "plain/held") == LANE_WAITERS_PER_THREAD + 1
    assert (served.status_code, served.json()) == (200, {"data": "held"})
