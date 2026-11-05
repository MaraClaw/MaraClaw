"""Live PostgreSQL + loopback S3 world for managed-storage quota tests."""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from uuid import UUID, uuid4

import pytest

from agent_deletion_fixtures import DeletionScenario
from app.dao.tenant_dao import tenant_dao
from app.records.agent_storage import StorageObjectRecord
from app.records.organization_plan import OrganizationPlan, OrganizationPlanId
from app.services.storage_runtime import facade, quota_ledger, quota_transfer
from app.services.storage_runtime.base import StorageBackend
from app.services.storage_runtime.fallback import FallbackStorageBackend
from app.services.storage_runtime.local import LocalStorageBackend
from app.services.storage_runtime.quota_s3 import ManagedS3StorageBackend
from s3_wire_server import S3Server
from tier_limit_fixtures import approve_admission_for


@dataclass
class StorageWorld:
    """One isolated schema, tenants, a wire-level S3 provider and the production managed backend."""

    scenario: DeletionScenario
    s3: S3Server
    backend: ManagedS3StorageBackend
    monkeypatch: pytest.MonkeyPatch

    def scale(
        self, storage_bytes: int, file_bytes: int, per_tier: dict[OrganizationPlanId, tuple[int, int]] | None = None
    ) -> None:
        """Test-fixture policy: keep each tier's identity but shrink both ceilings so boundaries are cheap."""
        real = quota_ledger.plan_for

        def scaled(tier: OrganizationPlanId) -> OrganizationPlan:
            storage, file = (per_tier or {}).get(tier, (storage_bytes, file_bytes))
            return real(tier).model_copy(update={"storage_limit_bytes": storage, "max_file_size_bytes": file})

        self.monkeypatch.setattr(quota_ledger, "plan_for", scaled)
        self.monkeypatch.setattr(quota_transfer, "plan_for", scaled)

    async def tenant(self, tier: OrganizationPlanId | None) -> UUID:
        created = await tenant_dao.create(obj_in={"name": f"T{uuid4().hex[:8]}", "slug": f"t{uuid4().hex[:10]}"})
        if tier is not None:
            _ = await approve_admission_for(self.scenario.db, created.id, tier)
        return created.id

    async def agent(self, tier: OrganizationPlanId | None = OrganizationPlanId.PRO, owner: str = "owner") -> UUID:
        return await self.scenario.agent(owner, tenant_id=await self.tenant(tier))

    def key(self, agent_id: UUID, name: str = "workspace/uploads/report.bin") -> str:
        return f"{agent_id}/{name}"

    def stored(self, key: str) -> bytes | None:
        return self.s3.state.objects.get(key)

    async def usage(self, agent_id: UUID) -> int:
        row = await self.scenario.db.fetchone(
            "SELECT charged_bytes FROM agent_storage_usage WHERE agent_id = %(a)s", {"a": agent_id}
        )
        return 0 if row is None else int(str(row["charged_bytes"]))

    async def row(self, agent_id: UUID, name: str = "workspace/uploads/report.bin") -> StorageObjectRecord | None:
        fetched = await self.scenario.db.fetchone(
            "SELECT * FROM agent_storage_objects WHERE agent_id = %(a)s AND object_key = %(k)s",
            {"a": agent_id, "k": name},
        )
        return StorageObjectRecord.from_row(fetched) if fetched is not None else None

    async def assert_ledger_consistent(self) -> None:
        drift = await self.scenario.db.fetchall(
            "SELECT u.agent_id FROM agent_storage_usage u LEFT JOIN (SELECT agent_id, "
            "sum(GREATEST(committed_bytes, reserved_bytes)) AS total FROM agent_storage_objects GROUP BY agent_id) o "
            "USING (agent_id) WHERE u.charged_bytes <> COALESCE(o.total, 0)"
        )
        orphans = await self.scenario.db.fetchall(
            "SELECT DISTINCT o.agent_id FROM agent_storage_objects o "
            "WHERE NOT EXISTS (SELECT 1 FROM agent_storage_usage u WHERE u.agent_id = o.agent_id)"
        )
        assert drift == [], "usage total differs from the sum of object charges"
        assert orphans == [], "object rows without a usage row"

    def fallback(self, legacy_root: str) -> StorageBackend:
        return FallbackStorageBackend(primary=self.backend, fallback=LocalStorageBackend(legacy_root))


@pytest.fixture
async def quota_world(
    deletion_scenario: DeletionScenario, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[StorageWorld]:
    monkeypatch.setenv("AWS_MAX_ATTEMPTS", "9")
    server = S3Server()
    server.start()
    backend = ManagedS3StorageBackend(
        bucket="bucket", prefix="", endpoint_url=server.endpoint, access_key_id="key", secret_access_key="secret"
    )
    monkeypatch.setattr(facade, "_storage_backend", backend)
    world = StorageWorld(deletion_scenario, server, backend, monkeypatch)
    try:
        yield world
        await world.assert_ledger_consistent()
    finally:
        server.stop()
