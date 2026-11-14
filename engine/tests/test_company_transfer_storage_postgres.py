"""Paid company transfer against the destination's per-agent storage limits (opt-in ``BOOTSTRAP_TEST_DATABASE_URL``)."""

from collections.abc import Iterator
from uuid import UUID

import pytest

from app.records.company_transfer import TransferChoice
from app.records.organization_plan import OrganizationPlanId
from app.services.storage_runtime import facade
from app.services.storage_runtime.quota_errors import StorageQuotaExceededError
from app.services.storage_runtime.quota_s3 import ManagedS3StorageBackend
from company_admission_fixtures import AdmissionScenario, admission_scenario
from s3_wire_server import S3Server
from test_company_transfer_postgres import transfer_scenario
from test_company_transfer_tier_postgres import (
    _approve as approve,
    _seed as seed_agent,
    _tenant_of as tenant_of,
    revocations,
)
from tier_limit_fixtures import approve_admission_for

__all__ = ["admission_scenario", "revocations", "transfer_scenario"]

MIB = 1024 * 1024
PRO_FILE, PRO_TOTAL = 25 * MIB, 2048 * MIB
FULL_FILES = PRO_TOTAL // PRO_FILE


async def hold_in_ledger(scenario: AdmissionScenario, agent: UUID, sizes: list[int]) -> None:
    """Ledger state as earlier managed writes would have left it, without uploading gigabytes."""
    await scenario.db.executemany(
        "INSERT INTO agent_storage_objects (agent_id, object_key, committed_bytes) VALUES (%(a)s, %(k)s, %(s)s)",
        [{"a": agent, "k": f"file-{index}", "s": size} for index, size in enumerate(sizes)],
    )
    await scenario.db.execute(
        "INSERT INTO agent_storage_usage (agent_id, charged_bytes) VALUES (%(a)s, %(t)s)",
        {"a": agent, "t": sum(sizes)},
    )


@pytest.fixture
def provider() -> Iterator[S3Server]:
    server = S3Server()
    server.start()
    yield server
    server.stop()


@pytest.mark.parametrize(
    "sizes",
    [[PRO_FILE] * FULL_FILES + [PRO_TOTAL - FULL_FILES * PRO_FILE + 1], [PRO_FILE + 1]],
    ids=["total-over", "largest-file-over"],
)
async def test_an_agent_holding_more_than_the_destination_allows_refuses_the_transfer_before_any_write(
    transfer_scenario: AdmissionScenario, revocations: list[UUID], sizes: list[int]
):
    scenario = transfer_scenario
    _ = await approve_admission_for(scenario.db, scenario.target_id, OrganizationPlanId.PRO)
    heavy = await seed_agent(scenario, "Heavy")
    light = await seed_agent(scenario, "Light")
    await hold_in_ledger(scenario, heavy, sizes)
    response = await approve(scenario, TransferChoice.PAID)
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "transfer_destination_storage_exceeded"
    assert {await tenant_of(scenario, heavy), await tenant_of(scenario, light)} == {scenario.source_id}
    assert (await scenario.current_applicant()).tenant_id == scenario.source_id
    assert revocations == []


async def test_exact_fit_transfers_and_later_writes_obey_the_destination_tier(
    transfer_scenario: AdmissionScenario,
    revocations: list[UUID],
    provider: S3Server,
    monkeypatch: pytest.MonkeyPatch,
):
    scenario = transfer_scenario
    _ = await approve_admission_for(scenario.db, scenario.target_id, OrganizationPlanId.PRO)
    agent = await seed_agent(scenario, "Exact")
    await hold_in_ledger(scenario, agent, [PRO_FILE] * FULL_FILES + [PRO_TOTAL - FULL_FILES * PRO_FILE])
    response = await approve(scenario, TransferChoice.PAID)
    assert response.status_code == 200, response.text
    assert await tenant_of(scenario, agent) == scenario.target_id
    backend = ManagedS3StorageBackend(
        bucket="bucket", endpoint_url=provider.endpoint, access_key_id="key", secret_access_key="secret"
    )
    monkeypatch.setattr(facade, "_storage_backend", backend)
    with pytest.raises(StorageQuotaExceededError):
        await backend.write_bytes(f"{agent}/one-more-byte", b"x")


async def test_an_untiered_destination_does_not_inspect_stored_bytes(
    transfer_scenario: AdmissionScenario, revocations: list[UUID]
):
    scenario = transfer_scenario
    agent = await seed_agent(scenario, "Heavy")
    await hold_in_ledger(scenario, agent, [PRO_FILE + 1])
    response = await approve(scenario, TransferChoice.PAID)
    assert response.status_code == 200, response.text
    assert await tenant_of(scenario, agent) == scenario.target_id
