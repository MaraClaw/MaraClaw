from datetime import datetime
from typing import Literal, NotRequired
from uuid import UUID

from typing_extensions import TypedDict

from app.schemas.schemas import UserOut


class SSOSessionResponse(TypedDict):
    session_id: str
    expires_at: datetime


class SSOUserPayload(TypedDict):
    id: UUID
    identity_id: UUID | None
    email: str | None
    display_name: str
    avatar_url: str | None
    role: str
    is_platform_admin: bool
    tenant_id: UUID | None
    title: str | None
    timezone: str | None
    primary_mobile: str | None
    registration_source: str | None
    is_active: bool
    is_genesis: bool
    email_verified: bool
    must_change_password: bool
    created_at: datetime


def sso_user_payload(user: UserOut) -> SSOUserPayload:
    return {
        "id": user.id,
        "identity_id": user.identity_id,
        "email": user.email,
        "display_name": user.display_name,
        "avatar_url": user.avatar_url,
        "role": user.role,
        "is_platform_admin": user.is_platform_admin,
        "tenant_id": user.tenant_id,
        "title": user.title,
        "timezone": user.timezone,
        "primary_mobile": user.primary_mobile,
        "registration_source": user.registration_source,
        "is_active": user.is_active,
        "is_genesis": user.is_genesis,
        "email_verified": user.email_verified,
        "must_change_password": user.must_change_password,
        "created_at": user.created_at,
    }


class SSOStatusResponse(TypedDict):
    status: str
    provider_type: str | None
    error_msg: str | None
    access_token: NotRequired[str]
    user: NotRequired[SSOUserPayload]


class SSOScannedResponse(TypedDict):
    status: Literal["ok"]


class SSOProviderResponse(TypedDict):
    provider_type: Literal["feishu", "dingtalk", "wecom", "google_workspace"]
    name: str
    url: str
