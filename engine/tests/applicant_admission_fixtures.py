from base64 import urlsafe_b64encode
from datetime import UTC, datetime
from json import dumps
from typing import ClassVar, assert_never
from uuid import UUID

from pydantic import BaseModel, ConfigDict, JsonValue

from app.dao.company_admission_dao import company_admission_dao
from app.records.company_admission import AdmissionStatus
from app.schemas.company_admission import RequestSummary
from company_admission_fixtures import AdmissionScenario

STAMP = datetime(2026, 10, 21, 12, 0, 0, 123456, tzinfo=UTC)
PAGE_PATH = "/api/company-admissions/mine/page"


class ApplicantSummaryResult(RequestSummary):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True)


class ApplicantPageResult(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True)

    items: list[ApplicantSummaryResult]
    next_cursor: str | None


def applicant_cursor(changes: dict[str, JsonValue] | None = None) -> str:
    payload: dict[str, JsonValue] = {
        "version": 1,
        "kind": "applicant",
        "user_id": str(UUID(int=1000)),
        "status": None,
        "created_at": STAMP.isoformat(),
        "id": str(UUID(int=2)),
    }
    payload.update(changes or {})
    return urlsafe_b64encode(dumps(payload).encode()).decode().rstrip("=")


async def seed_applicant(
    scenario: AdmissionScenario,
    positions: list[tuple[int, datetime]],
    status: AdmissionStatus = AdmissionStatus.REJECTED,
) -> list[UUID]:
    ids: list[UUID] = []
    for number, created_at in positions:
        match status:
            case AdmissionStatus.PENDING:
                decided_at = None
            case AdmissionStatus.APPROVED | AdmissionStatus.REJECTED | AdmissionStatus.WITHDRAWN:
                decided_at = created_at
            case unreachable:
                assert_never(unreachable)
        request = await company_admission_dao.create(
            obj_in={
                "id": UUID(int=number),
                "applicant_user_id": scenario.applicant.id,
                "source_tenant_id": scenario.source_id,
                "target_tenant_id": scenario.target_id,
                "method": "verified_domain",
                "status": status.value,
                "created_at": created_at,
                "decided_at": decided_at,
            }
        )
        ids.append(request.id)
    return ids


async def applicant_page(
    scenario: AdmissionScenario, params: dict[str, str | int] | None = None
) -> ApplicantPageResult:
    response = await scenario.client.get(PAGE_PATH, params=params, headers=scenario.headers(scenario.applicant))
    assert response.status_code == 200, response.text
    return ApplicantPageResult.model_validate_json(response.content)
