from datetime import UTC, datetime
from unittest.mock import AsyncMock
from uuid import UUID

import anyio
import pytest
from pydantic import TypeAdapter

from app.core.security import verify_password_async
from app.dao.organization_admission_dao import organization_admission_dao
from app.dao.user_dao import user_dao
from app.db.errors import CheckViolationError
from app.db.types import as_jsonb
from app.records.organization_plan import OrganizationPlanId
from app.services import organization_admissions
from app.services.organization_admission_credentials import unseal
from app.services.organization_plans import plan_for
from app.services.quota_guard import agent_capacity
from organization_admission_fixtures import OrganizationScenario, organization_scenario

__all__ = ["organization_scenario"]

APPROVE = {"decision": "approve", "note": "  paid offline  "}
COUNTS = (
    "SELECT (SELECT count(*) FROM tenants) AS tenants, (SELECT count(*) FROM users) AS users, "
    "(SELECT count(*) FROM identities) AS identities, (SELECT count(*) FROM tenant_email_domains) AS domains, "
    "(SELECT count(*) FROM organization_admission_credential_emails) AS intents"
)


async def _counts(scenario: OrganizationScenario) -> dict[str, int]:
    row = await scenario.db.fetchone(COUNTS)
    assert row is not None
    return TypeAdapter(dict[str, int]).validate_python(row)


async def _submit_and_approve(scenario: OrganizationScenario, name: str = "Acme Robotics") -> UUID:
    _ = await scenario.submit(name=name)
    request_id = await scenario.pending_id()
    assert (await scenario.decide(request_id, scenario.genesis, APPROVE)).status_code == 200
    return request_id


async def _issued_password(scenario: OrganizationScenario, request_id: UUID) -> str:
    payload = await scenario.db.fetchval(
        "SELECT payload FROM organization_admission_credential_emails WHERE request_id=%(id)s", {"id": request_id}
    )
    assert isinstance(payload, bytes)
    return unseal(request_id, payload)


@pytest.mark.parametrize(("tier", "cap"), [("pro", 2), ("max", 3), ("ultra", 5)])
async def test_approval_links_the_tenant_to_its_tier_and_replay_keeps_one_link(
    organization_scenario: OrganizationScenario, tier: str, cap: int
):
    scenario = organization_scenario
    _ = await scenario.submit(tier=tier)
    request_id = await scenario.pending_id()
    assert (await scenario.decide(request_id, scenario.genesis, APPROVE)).status_code == 200
    assert (await scenario.decide(request_id, scenario.genesis, APPROVE)).status_code == 200
    row = await scenario.db.fetchone(
        "SELECT provisioned_tenant_id, provisioned_admin_user_id FROM organization_admission_requests WHERE id=%(id)s",
        {"id": request_id},
    )
    assert row is not None
    tenant_id, admin_id = row["provisioned_tenant_id"], row["provisioned_admin_user_id"]
    assert isinstance(tenant_id, UUID)
    assert isinstance(admin_id, UUID)
    assert await organization_admission_dao.approved_tiers_for_tenant(tenant_id) == [OrganizationPlanId(tier)]
    admin = await user_dao.get(admin_id)
    assert admin is not None
    capacity = await agent_capacity(admin, tenant_id)
    assert capacity is not None
    assert (capacity.limit, capacity.tiered) == (cap, True)


async def test_approval_provisions_the_named_organization_and_queues_one_encrypted_credential(
    organization_scenario: OrganizationScenario,
):
    scenario = organization_scenario
    _ = await scenario.submit(name="Acme <b>Robotics</b> & Sons")
    request_id = await scenario.pending_id()
    before = await _counts(scenario)
    response = await scenario.decide(request_id, scenario.genesis, APPROVE)
    assert response.status_code == 200
    body = response.json()
    assert (body["status"], body["review_note"], body["reviewed_by"], body["organization_name"]) == (
        "approved",
        "paid offline",
        str(scenario.genesis.id),
        "Acme <b>Robotics</b> & Sons",
    )
    assert (body["credential_email_status"], body["credential_email_error"]) == ("pending", None)
    after = await _counts(scenario)
    assert {key: after[key] - before[key] for key in after} == {
        "tenants": 1,
        "users": 1,
        "identities": 1,
        "domains": 1,
        "intents": 1,
    }
    tenant = await scenario.db.fetchone(
        "SELECT id,name FROM tenants WHERE name=%(name)s", {"name": body["organization_name"]}
    )
    admin = await scenario.db.fetchone(
        "SELECT u.id,u.role,u.is_genesis,u.is_active,i.id AS identity_id,i.email,i.email_verified,"
        "i.must_change_password,i.password_hash FROM users u JOIN identities i ON i.id=u.identity_id "
        "WHERE u.tenant_id=%(tenant)s",
        {"tenant": tenant["id"] if tenant is not None else None},
    )
    assert tenant is not None
    assert admin is not None
    assert (admin["role"], admin["is_genesis"], admin["is_active"], admin["email"]) == (
        "org_admin",
        True,
        True,
        "buyer@example.com",
    )
    assert (admin["email_verified"], admin["must_change_password"]) == (True, True)
    request = await scenario.db.fetchone(
        "SELECT * FROM organization_admission_requests WHERE id=%(id)s", {"id": request_id}
    )
    assert request is not None
    assert (request["provisioned_tenant_id"], request["provisioned_admin_user_id"]) == (tenant["id"], admin["id"])
    assert request["provisioned_at"] is not None
    password = await _issued_password(scenario, request_id)
    assert len(password) >= 24
    assert isinstance(admin["password_hash"], str)
    assert await verify_password_async(password, admin["password_hash"])
    assert (
        await scenario.db.fetchval(
            "SELECT count(*) FROM admin_audit_logs WHERE action='tenant_create' AND target_id=%(id)s",
            {"id": tenant["id"]},
        )
        == 1
    )
    assert password not in response.text
    for table in ("organization_admission_requests", "notifications", "admin_audit_logs", "identities"):
        leaked = await scenario.db.fetchval(
            f"SELECT count(*) FROM {table} t WHERE position(%(pw)s in t::text) > 0",  # noqa: S608
            {"pw": password},
        )
        assert leaked == 0, table
    intent = await scenario.db.fetchone("SELECT * FROM organization_admission_credential_emails")
    assert intent is not None
    assert (intent["status"], intent["attempts"], intent["to_email"], intent["identity_id"]) == (
        "pending",
        0,
        "buyer@example.com",
        admin["identity_id"],
    )
    assert intent["message_id"] == f"<{request_id}.org-admission@maraclaw.local>"
    assert isinstance(intent["payload"], bytes)
    assert password.encode() not in intent["payload"]


async def test_replayed_and_concurrent_approvals_provision_exactly_once_without_rotating_credentials(
    organization_scenario: OrganizationScenario,
):
    scenario = organization_scenario
    _ = await scenario.submit()
    request_id = await scenario.pending_id()
    before = await _counts(scenario)
    bodies: list[dict[str, object]] = []

    async def approve() -> None:
        response = await scenario.decide(request_id, scenario.genesis, APPROVE)
        assert response.status_code == 200
        bodies.append(response.json())

    async with anyio.create_task_group() as group:
        for _ in range(3):
            group.start_soon(approve)
    password = await _issued_password(scenario, request_id)
    after = await _counts(scenario)
    assert {key: after[key] - before[key] for key in after} == {
        "tenants": 1,
        "users": 1,
        "identities": 1,
        "domains": 1,
        "intents": 1,
    }
    assert all(body == bodies[0] for body in bodies)
    again = await scenario.decide(request_id, scenario.genesis, {"decision": "approve", "note": "ignored"})
    assert again.json() == bodies[0]
    assert await _counts(scenario) == after
    assert await _issued_password(scenario, request_id) == password
    assert (await scenario.decide(request_id, scenario.genesis, {"decision": "reject"})).status_code == 409


@pytest.mark.parametrize(
    ("conflict", "detail"),
    [("identity", "Admin email is already registered"), ("domain", "Email domain is already claimed")],
)
async def test_identity_or_domain_conflict_is_a_sanitized_409_that_rolls_everything_back(
    organization_scenario: OrganizationScenario, conflict: str, detail: str
):
    scenario = organization_scenario
    if conflict == "identity":
        await scenario.db.execute(
            "INSERT INTO identities (id,email,password_hash) VALUES (gen_random_uuid(),'buyer@example.com','existing-hash')"
        )
    else:
        await scenario.db.execute(
            "INSERT INTO tenant_email_domains (id,tenant_id,domain) SELECT gen_random_uuid(),id,'example.com' FROM tenants WHERE slug='acme'"
        )
    _ = await scenario.submit()
    request_id = await scenario.pending_id()
    before = await _counts(scenario)
    response = await scenario.decide(request_id, scenario.genesis, APPROVE)
    assert (response.status_code, response.json()) == (409, {"detail": detail})
    assert await _counts(scenario) == before
    row = await scenario.db.fetchone("SELECT status,provisioned_at FROM organization_admission_requests")
    assert row == {"status": "pending", "provisioned_at": None}
    if conflict == "identity":
        existing = await scenario.db.fetchone(
            "SELECT password_hash,must_change_password FROM identities WHERE email='buyer@example.com'"
        )
        assert existing == {"password_hash": "existing-hash", "must_change_password": False}
    assert (await scenario.decide(request_id, scenario.genesis, {"decision": "reject"})).status_code == 200


async def test_failure_after_tenant_creation_rolls_back_the_whole_approval(
    organization_scenario: OrganizationScenario, monkeypatch: pytest.MonkeyPatch
):
    scenario = organization_scenario
    _ = await scenario.submit()
    request_id = await scenario.pending_id()
    before = await _counts(scenario)
    with monkeypatch.context() as patch:
        patch.setattr(organization_admissions.emails, "enqueue", AsyncMock(side_effect=RuntimeError("injected")))
        with pytest.raises(RuntimeError, match="injected"):
            _ = await scenario.decide(request_id, scenario.genesis, APPROVE)
    assert await _counts(scenario) == before
    assert await scenario.db.fetchval("SELECT status FROM organization_admission_requests") == "pending"
    assert (await scenario.decide(request_id, scenario.genesis, APPROVE)).status_code == 200
    assert (await _counts(scenario))["tenants"] == before["tenants"] + 1


async def test_rejection_provisions_nothing_and_has_no_credential_email_to_retry(
    organization_scenario: OrganizationScenario,
):
    scenario = organization_scenario
    _ = await scenario.submit()
    request_id = await scenario.pending_id()
    before = await _counts(scenario)
    response = await scenario.decide(request_id, scenario.genesis, {"decision": "reject"})
    body = response.json()
    assert (body["status"], body["credential_email_status"], body["credential_email_error"]) == ("rejected", None, None)
    assert await _counts(scenario) == before
    assert (await scenario.retry_email(request_id, scenario.genesis)).status_code == 409


async def test_legacy_unnamed_requests_are_never_guessed_or_provisioned(organization_scenario: OrganizationScenario):
    scenario = organization_scenario
    plan = as_jsonb(plan_for(OrganizationPlanId.PRO).model_dump(mode="json"))
    for email, status, reviewed_at in (
        ("old-approved@example.com", "approved", datetime.now(UTC)),
        ("old-pending@example.com", "pending", None),
    ):
        await scenario.db.execute(
            "INSERT INTO organization_admission_requests (id,email,tier,plan,status,reviewed_at) "
            "VALUES (gen_random_uuid(),%(email)s,'pro',%(plan)s,%(status)s,%(reviewed_at)s)",
            {"email": email, "plan": plan, "status": status, "reviewed_at": reviewed_at},
        )
    approved_id = await scenario.db.fetchval("SELECT id FROM organization_admission_requests WHERE status='approved'")
    pending_id = await scenario.pending_id()
    before = await _counts(scenario)
    replay = await scenario.decide(approved_id, scenario.genesis, {"decision": "approve"})
    assert replay.status_code == 200
    assert (replay.json()["organization_name"], replay.json()["credential_email_status"]) == (None, None)
    refused = await scenario.decide(pending_id, scenario.genesis, APPROVE)
    assert refused.status_code == 409
    assert "no organization name" in refused.json()["detail"]
    assert await _counts(scenario) == before
    assert (await scenario.retry_email(approved_id, scenario.genesis)).status_code == 409
    assert (await scenario.decide(pending_id, scenario.genesis, {"decision": "reject"})).status_code == 200


async def test_database_rejects_incoherent_provisioning_state(organization_scenario: OrganizationScenario):
    scenario = organization_scenario
    _ = await scenario.submit()
    for statement in (
        "UPDATE organization_admission_requests SET status='approved',reviewed_at=now()",
        "UPDATE organization_admission_requests SET status='rejected',reviewed_at=now(),provisioned_at=now()",
        "UPDATE organization_admission_requests SET organization_name=' padded '",
        "UPDATE organization_admission_requests SET provisioned_tenant_id=(SELECT id FROM tenants LIMIT 1)",
    ):
        with pytest.raises(CheckViolationError):
            await scenario.db.execute(statement)


@pytest.mark.parametrize("actor", ["additional", "org_genesis", "member"])
async def test_only_genesis_platform_admin_may_retry_credential_email(
    organization_scenario: OrganizationScenario, actor: str
):
    scenario = organization_scenario
    request_id = await _submit_and_approve(scenario)
    assert (await scenario.retry_email(request_id, getattr(scenario, actor))).status_code == 403
    anonymous = await scenario.client.post(f"/api/admin/organization-admissions/{request_id}/retry-email")
    assert anonymous.status_code == 401
