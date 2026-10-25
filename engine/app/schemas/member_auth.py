"""Member auth projections, independent of shared/operator DTOs."""

from datetime import datetime
from typing import ClassVar, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.core.domain.users import UserRole
from app.schemas.company_admission import RequestSummary
from app.schemas.schemas import SuggestedOrg
from app.services.auth_results import AuthRegistration, AuthSession


class MemberAuthUser(BaseModel):
    id: UUID
    identity_id: UUID | None = None
    email: str | None = None
    display_name: str
    avatar_url: str | None = None
    role: Literal[UserRole.ORG_ADMIN, UserRole.AGENT_ADMIN, UserRole.MEMBER]
    tenant_id: UUID | None = None
    title: str | None = None
    timezone: str | None = None
    primary_mobile: str | None = None
    registration_source: str | None = None
    is_active: bool
    is_genesis: bool = False
    email_verified: bool = True
    must_change_password: bool = False
    created_at: datetime

    model_config: ClassVar[ConfigDict] = ConfigDict(from_attributes=True)


class MemberIdentityOut(BaseModel):
    id: UUID
    email: str | None = None
    phone: str | None = None
    is_active: bool
    email_verified: bool
    must_change_password: bool = False
    created_at: datetime
    updated_at: datetime

    model_config: ClassVar[ConfigDict] = ConfigDict(from_attributes=True)


class MemberTokenResponse(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = Field(default="bearer")
    user: MemberAuthUser | None = None
    identity: MemberIdentityOut | None = None
    tenant_name: str | None = None
    must_change_password: bool = False
    needs_org_confirm: bool = False
    suggested_org: SuggestedOrg | None = None

    @classmethod
    def from_session(cls, session: AuthSession) -> MemberTokenResponse:
        return cls(
            access_token=session.access_token,
            user=MemberAuthUser.model_validate(session.user) if session.user else None,
            identity=MemberIdentityOut.model_validate(session.identity) if session.identity else None,
            must_change_password=session.must_change_password,
            needs_org_confirm=session.needs_org_confirm,
            suggested_org=session.suggested_org,
        )


class MemberRegisterInitResponse(BaseModel):
    user_id: UUID
    email: str
    access_token: str
    message: str
    user: MemberAuthUser
    target_tenant_id: UUID | None = None
    needs_org_confirm: bool = False
    suggested_org: SuggestedOrg | None = None
    admission_request: RequestSummary | None = None

    @classmethod
    def from_registration(cls, registration: AuthRegistration) -> MemberRegisterInitResponse:
        return cls(
            user_id=registration.user.id,
            email=registration.email,
            access_token=registration.access_token,
            message=registration.message,
            user=MemberAuthUser.model_validate(registration.user),
            target_tenant_id=registration.target_tenant_id,
            needs_org_confirm=registration.needs_org_confirm,
            suggested_org=registration.suggested_org,
            admission_request=registration.admission_request,
        )
