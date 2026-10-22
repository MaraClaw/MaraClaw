from unittest.mock import AsyncMock
from uuid import UUID

import anyio
import pytest

from app.dao.tenant_dao import tenant_dao
from app.db.connection import DbConnection
from app.records.company_admission import CompanyAdmissionRecord
from app.services import company_admission_decisions, company_admissions
from company_admission_fixtures import AdmissionScenario, admission_scenario

__all__ = ["admission_scenario"]


async def test_notification_failure_rolls_back_request(
    admission_scenario: AdmissionScenario, monkeypatch: pytest.MonkeyPatch
):
    scenario = admission_scenario
    monkeypatch.setattr(
        company_admissions, "send_notification", AsyncMock(side_effect=RuntimeError("injected notification failure"))
    )
    with pytest.raises(RuntimeError, match="injected notification failure"):
        await scenario.submit()
    assert await scenario.db.fetchval("SELECT count(*) FROM company_admission_requests") == 0
    assert (await scenario.current_applicant()).tenant_id == scenario.source_id
    assert await scenario.db.fetchval("SELECT count(*) FROM admin_audit_logs") == 0


async def test_notification_failure_rolls_back_approval(
    admission_scenario: AdmissionScenario, monkeypatch: pytest.MonkeyPatch
):
    scenario = admission_scenario
    request_id = await scenario.submit()
    monkeypatch.setattr(
        company_admission_decisions,
        "send_notification",
        AsyncMock(side_effect=RuntimeError("injected notification failure")),
    )
    with pytest.raises(RuntimeError, match="injected notification failure"):
        await scenario.client.post(
            f"/api/company-admissions/{request_id}/decision",
            json={"decision": "approved"},
            headers=scenario.headers(scenario.genesis),
        )
    assert await scenario.db.fetchval("SELECT status FROM company_admission_requests") == "pending"
    assert await scenario.db.fetchval("SELECT used_count FROM invitation_codes WHERE code='JOIN'") == 0
    assert (await scenario.current_applicant()).tenant_id == scenario.source_id
    assert await scenario.db.fetchval("SELECT count(*) FROM admin_audit_logs") == 0


async def test_parallel_duplicate_requests_are_serialized(admission_scenario: AdmissionScenario):
    scenario = admission_scenario
    results: list[str] = []

    async def submit() -> None:
        results.append(await scenario.submit())

    async with anyio.create_task_group() as group:
        group.start_soon(submit)
        group.start_soon(submit)
    assert len(set(results)) == 1
    assert await scenario.db.fetchval("SELECT count(*) FROM notifications") == 1


async def test_fresh_demoted_genesis_cannot_decide(admission_scenario: AdmissionScenario):
    scenario = admission_scenario
    request_id = await scenario.submit()
    await scenario.db.execute("UPDATE users SET role='member' WHERE id=%(id)s", {"id": scenario.genesis.id})
    response = await scenario.client.post(
        f"/api/company-admissions/{request_id}/decision",
        json={"decision": "approved"},
        headers=scenario.headers(scenario.genesis),
    )
    assert response.status_code == 403
    assert await scenario.db.fetchval("SELECT status FROM company_admission_requests") == "pending"
    assert await scenario.db.fetchval("SELECT count(*) FROM admin_audit_logs") == 0


async def test_parallel_conflicting_decisions_have_one_winner(admission_scenario: AdmissionScenario):
    scenario = admission_scenario
    request_id = await scenario.submit()
    results: list[int] = []

    async def decide(decision: str) -> None:
        response = await scenario.client.post(
            f"/api/company-admissions/{request_id}/decision",
            json={"decision": decision},
            headers=scenario.headers(scenario.genesis),
        )
        results.append(response.status_code)

    async with anyio.create_task_group() as group:
        group.start_soon(decide, "approved")
        group.start_soon(decide, "rejected")
    assert sorted(results) == [200, 409]
    assert await scenario.db.fetchval("SELECT count(*) FROM notifications WHERE type='company_admission_resolved'") == 1
    assert await scenario.db.fetchval("SELECT count(*) FROM admin_audit_logs") == 1


@pytest.mark.parametrize("tenant", ["source_id", "target_id"])
async def test_company_deletion_cleans_pending_admission(admission_scenario: AdmissionScenario, tenant: str):
    scenario = admission_scenario
    await scenario.submit()
    tenants = {"source_id": scenario.source_id, "target_id": scenario.target_id}
    await tenant_dao.delete_cascade(tenants[tenant])
    assert await scenario.db.fetchval("SELECT count(*) FROM company_admission_requests") == 0


@pytest.mark.parametrize("action", ["withdraw", "decision"])
async def test_response_failure_rolls_back_transition(
    admission_scenario: AdmissionScenario, monkeypatch: pytest.MonkeyPatch, action: str
):
    # Given a failure after the service update, before the HTTP transaction commits.
    from app.api import company_admissions as admission_api

    scenario = admission_scenario
    request_id = await scenario.submit()
    monkeypatch.setattr(admission_api, "detail", AsyncMock(side_effect=RuntimeError("injected detail failure")))

    # When response assembly fails.
    with pytest.raises(RuntimeError, match="injected detail failure"):
        await scenario.client.post(
            f"/api/company-admissions/{request_id}/{action}",
            json={"decision": "approved"} if action == "decision" else None,
            headers=scenario.headers(scenario.genesis if action == "decision" else scenario.applicant),
        )

    # Then no part of the withdrawal survives the rollback.
    assert await scenario.db.fetchone(
        "SELECT status,decided_at,decided_by_user_id FROM company_admission_requests"
    ) == {
        "status": "pending",
        "decided_at": None,
        "decided_by_user_id": None,
    }
    assert (await scenario.current_applicant()).tenant_id == scenario.source_id
    assert await scenario.db.fetchval("SELECT used_count FROM invitation_codes WHERE code='JOIN'") == 0
    assert await scenario.db.fetchval("SELECT count(*) FROM admin_audit_logs") == 0


@pytest.mark.parametrize("decision", ["approved", "rejected"])
@pytest.mark.parametrize("first", ["decision", "withdraw"])
async def test_withdrawal_and_reviewer_decision_have_one_terminal_winner(
    admission_scenario: AdmissionScenario, monkeypatch: pytest.MonkeyPatch, decision: str, first: str
):
    # Given a real PostgreSQL transaction paused while holding its ordered row locks.
    scenario = admission_scenario
    request_id = await scenario.submit()
    locked = anyio.Event()
    contender = anyio.Event()
    release = anyio.Event()
    get_locked = company_admissions.admissions.get_locked
    lock_tenants = company_admissions.lock_tenants
    results: dict[str, int] = {}

    async def pause_first(request_id: UUID) -> CompanyAdmissionRecord | None:
        request = await get_locked(request_id)
        if not locked.is_set():
            locked.set()
            await release.wait()
        return request

    async def mark_contender(db: DbConnection, ids: tuple[UUID | None, ...]) -> None:
        if locked.is_set():
            contender.set()
        await lock_tenants(db, ids)

    monkeypatch.setattr(company_admissions.admissions, "get_locked", pause_first)
    monkeypatch.setattr(company_admissions, "lock_tenants", mark_contender)
    monkeypatch.setattr(company_admission_decisions, "lock_tenants", mark_contender)

    async def transition(action: str) -> None:
        user = scenario.applicant if action == "withdraw" else scenario.genesis
        response = await scenario.client.post(
            f"/api/company-admissions/{request_id}/{action}",
            json={"decision": decision} if action == "decision" else None,
            headers=scenario.headers(user),
        )
        results[action] = response.status_code

    # When the competing HTTP transaction begins before the first one is released.
    with anyio.fail_after(10):
        async with anyio.create_task_group() as group:
            group.start_soon(transition, first)
            await locked.wait()
            group.start_soon(transition, "withdraw" if first == "decision" else "decision")
            await contender.wait()
            release.set()

    # Then exactly one terminal result and only that result's side effects commit.
    expected = decision if first == "decision" else "withdrawn"
    assert results[first] == 200
    assert sorted(results.values()) == [200, 409]
    assert await scenario.db.fetchval("SELECT status FROM company_admission_requests") == expected
    approved = expected == "approved"
    assert (await scenario.current_applicant()).tenant_id == (scenario.target_id if approved else scenario.source_id)
    assert await scenario.db.fetchval("SELECT used_count FROM invitation_codes WHERE code='JOIN'") == int(approved)
    assert await scenario.db.fetchval(
        "SELECT count(*) FROM notifications WHERE type='company_admission_resolved'"
    ) == int(first == "decision")
    assert await scenario.db.fetchval("SELECT count(*) FROM admin_audit_logs") == int(first == "decision")


@pytest.mark.parametrize("decision", ["approved", "rejected"])
async def test_decision_audits_fresh_reviewer_once_on_replay(admission_scenario: AdmissionScenario, decision: str):
    # Given a reviewer whose identity changed since the initial principal was loaded.
    scenario = admission_scenario
    request_id = await scenario.submit()
    await scenario.db.execute(
        "UPDATE identities SET email='fresh-reviewer@acme.test' WHERE id=%(id)s",
        {"id": scenario.genesis.identity_id},
    )

    # When the same decision is submitted twice.
    for _ in range(2):
        response = await scenario.client.post(
            f"/api/company-admissions/{request_id}/decision",
            json={"decision": decision, "reason": "private decision context"},
            headers=scenario.headers(scenario.genesis),
        )
        assert response.status_code == 200, response.text

    # Then exactly one redacted transition is stored in the target company's trail.
    assert await scenario.db.fetchval("SELECT count(*) FROM admin_audit_logs") == 1
    assert await scenario.db.fetchone(
        "SELECT actor_id,actor_email,actor_role,tenant_id,action,target_type,target_id,changes,details "
        "FROM admin_audit_logs"
    ) == {
        "actor_id": scenario.genesis.id,
        "actor_email": "fresh-reviewer@acme.test",
        "actor_role": "org_admin",
        "tenant_id": scenario.target_id,
        "action": "company_admission.decided",
        "target_type": "company_admission",
        "target_id": UUID(request_id),
        "changes": {"status": {"before": "pending", "after": decision}},
        "details": {
            "applicant_user_id": str(scenario.applicant.id),
            "source_tenant_id": str(scenario.source_id),
            "method": "invitation",
        },
    }


async def test_decision_survives_real_audit_sql_failure(admission_scenario: AdmissionScenario):
    # Given a database constraint that rejects this audit event only.
    scenario = admission_scenario
    request_id = await scenario.submit()
    await scenario.db.execute(
        "ALTER TABLE admin_audit_logs ADD CONSTRAINT reject_decision_audit "
        "CHECK (action <> 'company_admission.decided')"
    )

    # When an approval reaches the best-effort writer's savepoint.
    response = await scenario.client.post(
        f"/api/company-admissions/{request_id}/decision",
        json={"decision": "approved"},
        headers=scenario.headers(scenario.genesis),
    )

    # Then approval, invitation use and notification still commit, without an audit.
    assert response.status_code == 200, response.text
    assert (await scenario.current_applicant()).tenant_id == scenario.target_id
    assert await scenario.db.fetchval("SELECT used_count FROM invitation_codes WHERE code='JOIN'") == 1
    assert await scenario.db.fetchval("SELECT count(*) FROM notifications WHERE type='company_admission_resolved'") == 1
    assert await scenario.db.fetchval("SELECT count(*) FROM admin_audit_logs") == 0


async def test_foreign_genesis_cannot_decide_or_create_audit(admission_scenario: AdmissionScenario):
    # Given a genesis reviewer moved out of the request's target company.
    scenario = admission_scenario
    request_id = await scenario.submit()
    await scenario.db.execute(
        "UPDATE users SET tenant_id=%(tenant)s WHERE id=%(id)s",
        {"tenant": scenario.source_id, "id": scenario.genesis.id},
    )

    # When the stale principal attempts the decision.
    response = await scenario.client.post(
        f"/api/company-admissions/{request_id}/decision",
        json={"decision": "rejected"},
        headers=scenario.headers(scenario.genesis),
    )

    # Then fresh tenant authority rejects both the mutation and its audit.
    assert response.status_code == 403
    assert await scenario.db.fetchval("SELECT status FROM company_admission_requests") == "pending"
    assert await scenario.db.fetchval("SELECT count(*) FROM admin_audit_logs") == 0
