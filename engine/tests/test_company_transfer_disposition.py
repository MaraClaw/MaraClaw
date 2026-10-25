from collections.abc import Mapping
from dataclasses import asdict
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException

from app.core.domain.agents import AgentStatus
from app.db.connection import DbConnection
from app.db.session import connection_ctx
from app.records.agent import AgentRecord
from app.records.company_admission import AdmissionMethod, CompanyAdmissionRecord
from app.records.company_transfer import CompanyTransferRecord, TransferChoice
from app.schemas.company_transfer import PaymentDecision
from app.services import company_transfer_cleanup, company_transfer_disposition as disposition
from app.services.company_transfer_requests import TransferDestination, TransferSelection, submit_transfer
from app.services.company_transfer_review import decide_payment
from company_transfer_fixture import TransferFixture, transfer_fixture

__all__ = ["transfer_fixture"]


@pytest.mark.parametrize("choice", list(TransferChoice))
async def test_final_disposition_stops_runtime_without_rewriting_history_ownership(
    transfer_fixture: TransferFixture, monkeypatch: pytest.MonkeyPatch, choice: TransferChoice
) -> None:
    # Given a real submitted disposition and an existing source agent with model/runtime access.
    fixture = transfer_fixture
    submitted = await submit_transfer(
        fixture.applicant, TransferDestination(fixture.target.id, AdmissionMethod.SSO), TransferSelection(choice, True)
    )
    if choice == TransferChoice.PAID:
        assert isinstance(submitted, CompanyTransferRecord)
        submitted = await decide_payment(submitted.id, fixture.reviewer.id, PaymentDecision(decision="approved"))
        assert submitted.admission_request_id is not None
        admission = fixture.admissions.rows[submitted.admission_request_id]
    else:
        assert isinstance(submitted, CompanyAdmissionRecord)
        admission = fixture.admissions.rows[submitted.id]
    agent = AgentRecord(
        id=uuid4(),
        name="Transfer",
        creator_id=fixture.applicant.id,
        tenant_id=fixture.source.id,
        api_key_hash="source-secret",
        primary_model_id=uuid4(),
        status=AgentStatus.RUNNING,
    )
    updates: list[AgentRecord] = []
    statements: list[str] = []

    async def list_agents(_id: UUID, *, tenant_id: UUID | None = None) -> list[AgentRecord]:
        return [agent] if tenant_id == fixture.source.id else []

    async def update(*, db_obj: AgentRecord, obj_in: Mapping[str, object]) -> AgentRecord:
        result = AgentRecord.from_row({**asdict(db_obj), **obj_in})
        updates.append(result)
        return result

    async def execute(_db: DbConnection, sql: str, _params: object = None) -> None:
        statements.append(sql)

    async def models(_db: DbConnection, _sql: str, _params: object = None) -> dict[str, object]:
        return {"primary_model_id": agent.primary_model_id, "secondary_model_id": None, "fallback_model_id": None}

    async def foreign_model(_db: DbConnection, _sql: str, _params: object = None) -> bool:
        return False

    async def no_runtime() -> None:
        return None

    monkeypatch.setattr(disposition, "transfers", fixture.transfers)
    monkeypatch.setattr(disposition.agent_dao, "list_for_creator", list_agents)
    monkeypatch.setattr(disposition.agent_dao, "update", update)
    monkeypatch.setattr(DbConnection, "execute", execute)
    monkeypatch.setattr(DbConnection, "fetchone", models)
    monkeypatch.setattr(DbConnection, "fetchval", foreign_model)
    monkeypatch.setattr(company_transfer_cleanup, "reconcile_transfer_cleanup", no_runtime)
    # When target approval applies that disposition in its transaction.
    async with connection_ctx():
        await disposition.apply_disposition(admission, fixture.applicant, frozenset({agent.id}))
    # Then runtime access is revoked and only free mode seals source ownership and deletes integrations.
    result = updates[0]
    assert result.status == "stopped"
    assert result.api_key_hash is None
    assert result.primary_model_id is None
    assert result.id == agent.id
    assert result.access_mode == "private"
    assert not result.heartbeat_enabled
    deleted_tables = {sql.split()[2] for sql in statements if sql.startswith("DELETE FROM")}
    assert not deleted_tables.intersection(
        {"agents", "chat_messages", "chat_sessions", "tasks", "audit_logs", "plaza_posts"}
    )
    if choice == TransferChoice.FREE:
        assert result.creator_id != agent.creator_id
        assert result.tenant_id == fixture.source.id
        assert result.is_expired
        assert {"agent_credentials", "agent_schedules", "published_pages"} <= deleted_tables
    else:
        assert result.creator_id == agent.creator_id
        assert result.tenant_id == fixture.target.id
        assert "agent_credentials" not in deleted_tables
        assert "agent_schedules" not in deleted_tables


async def test_implicit_admission_cannot_approve_after_assets_appear(
    transfer_fixture: TransferFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given an admission created while the default-org applicant had no agents.
    from app.services.company_admissions import submit_request

    fixture = transfer_fixture
    monkeypatch.setattr(disposition, "transfers", fixture.transfers)
    fixture.owned_agents = False
    admission = await submit_request(fixture.applicant, fixture.target.id, AdmissionMethod.SSO)
    fixture.owned_agents = True
    # When final approval rechecks the implicit disposition.
    with pytest.raises(HTTPException) as error:
        await disposition.apply_disposition(admission, fixture.applicant, frozenset())
    # Then no new assets can slip through an old agent-free request.
    assert error.value.detail == {"code": "transfer_choice_required"}
