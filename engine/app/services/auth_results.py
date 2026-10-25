"""Domain-record results shared by auth route audiences."""

from dataclasses import dataclass
from uuid import UUID

from app.records.identity import IdentityRecord
from app.records.user import UserRecord
from app.schemas.company_admission import RequestSummary
from app.schemas.schemas import SuggestedOrg


@dataclass(frozen=True, slots=True)
class AuthSession:
    access_token: str
    user: UserRecord | None
    identity: IdentityRecord | None = None
    must_change_password: bool = False
    needs_org_confirm: bool = False
    suggested_org: SuggestedOrg | None = None


@dataclass(frozen=True, slots=True)
class AuthRegistration:
    user: UserRecord
    email: str
    access_token: str
    message: str
    target_tenant_id: UUID | None
    needs_org_confirm: bool
    suggested_org: SuggestedOrg | None
    admission_request: RequestSummary | None
