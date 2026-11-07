"""Managed storage quota through the real ASGI app, auth and upload route (opt-in ``MARACLAW_TEST_POSTGRES_DSN``)."""

import asyncio
from collections.abc import AsyncIterator
from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient, Response

from agent_deletion_fixtures import deletion_scenario
from app.db.errors import DbError
from app.db.pool import close_pool, get_pool, init_pool
from app.main import app
from app.records.organization_plan import OrganizationPlanId
from app.services.storage_runtime import quota_ledger
from managed_storage_fixtures import StorageWorld, quota_world
from s3_wire_server import Gate
from tier_limit_fixtures import approve_admission_for

__all__ = ["deletion_scenario", "quota_world"]

LIMIT, CEILING = 1000, 400


@pytest.fixture
async def api() -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://quota.test") as client:
        yield client


async def paid_agent(world: StorageWorld, owner: str) -> UUID:
    """An agent of the scenario organization, which is linked to a paid tier."""
    _ = await approve_admission_for(world.scenario.db, world.scenario.tenant_id, OrganizationPlanId.PRO)
    return await world.scenario.agent(owner)


async def upload(world: StorageWorld, api: AsyncClient, who: str, agent: UUID, size: int) -> Response:
    return await api.post(
        "/api/chat/upload",
        headers=world.scenario.headers(who),
        data={"agent_id": str(agent)},
        files={"file": ("note.txt", b"a" * size, "text/plain")},
    )


async def test_an_upload_within_the_allowance_is_stored_and_charged(quota_world: StorageWorld, api: AsyncClient):
    world = quota_world
    world.scale(LIMIT, CEILING)
    agent = await paid_agent(world, "owner")
    response = await upload(world, api, "owner", agent, CEILING)
    assert response.status_code == 200, response.text
    assert (await world.usage(agent), response.json()["size"]) == (CEILING, CEILING)


async def test_a_file_over_the_tier_ceiling_is_413_with_a_safe_body(quota_world: StorageWorld, api: AsyncClient):
    world = quota_world
    world.scale(LIMIT, CEILING)
    agent = await paid_agent(world, "owner")
    response = await upload(world, api, "owner", agent, CEILING + 1)
    detail = response.json()["detail"]
    assert (response.status_code, detail["code"], detail["max_file_bytes"]) == (413, "storage_file_too_large", CEILING)
    assert world.s3.state.count("PUT") == 0


@pytest.mark.parametrize("who", ["owner", "helper", "genesis", "psa"])
async def test_the_allowance_binds_every_role_alike(quota_world: StorageWorld, api: AsyncClient, who: str):
    world = quota_world
    world.scale(LIMIT, CEILING)
    agent = await paid_agent(world, "owner")
    for name, size in (("a", 400), ("b", 400), ("c", 200)):
        await world.backend.write_bytes(world.key(agent, name), b"x" * size)
    response = await upload(world, api, who, agent, 1)
    detail = response.json()["detail"]
    assert (response.status_code, detail["code"], detail["storage_limit_bytes"]) == (
        507,
        "storage_quota_exceeded",
        LIMIT,
    )
    assert await world.usage(agent) == LIMIT


async def test_a_ledger_outage_is_503_without_provider_or_database_detail(
    quota_world: StorageWorld, api: AsyncClient, monkeypatch: pytest.MonkeyPatch
):
    world = quota_world
    world.scale(LIMIT, CEILING)
    agent = await paid_agent(world, "owner")

    def offline() -> None:
        raise DbError("postgresql://user:secret@db.internal/maraclaw refused")

    monkeypatch.setattr(quota_ledger, "durable_transaction", offline)
    response = await upload(world, api, "owner", agent, 10)
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "storage_accounting_unavailable"
    assert "secret" not in response.text
    assert world.s3.state.count("PUT") == 0


async def test_an_organization_without_a_paid_tier_has_no_file_or_total_ceiling(
    quota_world: StorageWorld, api: AsyncClient
):
    world = quota_world
    world.scale(LIMIT, CEILING)
    agent = await world.scenario.agent("owner")
    response = await upload(world, api, "owner", agent, CEILING * 4)
    assert response.status_code == 200, response.text
    assert await world.usage(agent) == CEILING * 4


@pytest.mark.parametrize("surface", ["chat", "workspace"])
async def test_a_blocked_upload_leaves_the_single_pool_slot_available(
    quota_world: StorageWorld, api: AsyncClient, surface: str
):
    world = quota_world
    world.scale(LIMIT, CEILING)
    agent = await paid_agent(world, "owner")
    conninfo = get_pool().conninfo
    assert isinstance(conninfo, str)
    await close_pool()
    pool = await init_pool(conninfo=conninfo, min_size=1, max_size=1)
    await pool.wait()
    assert pool.get_stats()["pool_size"] == 1
    gate = Gate()
    world.s3.state.gates[world.key(agent, "workspace/uploads/note.txt")] = gate

    async def send() -> Response:
        if surface == "chat":
            return await upload(world, api, "owner", agent, 10)
        return await api.post(
            f"/api/agents/{agent}/files/upload",
            params={"path": "workspace/uploads"},
            headers=world.scenario.headers("owner"),
            files={"file": ("note.txt", b"a" * 10, "text/plain")},
        )

    pending = asyncio.create_task(send())
    try:
        assert await asyncio.to_thread(gate.arrived.wait, 5), "upload never reached the S3 provider"
        async with asyncio.timeout(5):
            response = await api.get("/api/departments", headers=world.scenario.headers("genesis"))
        assert response.status_code == 200, response.text
        assert response.json() == {"departments": [], "assignments": []}
        assert not pending.done(), "upload must still be blocked while the other request completes"
    finally:
        gate.release.set()
        uploaded = await pending
    assert uploaded.status_code == 200, uploaded.text
    assert await world.usage(agent) == 10
