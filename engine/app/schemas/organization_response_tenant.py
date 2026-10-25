from typing import Literal
from uuid import UUID

from typing_extensions import TypedDict

from app.core.domain.users import UserRole


class TenantRegistrationResponse(TypedDict):
    tenant_creation: Literal["platform_admin_only"]


class TenantResolvedResponse(TypedDict):
    id: UUID
    name: str
    slug: str
    sso_enabled: bool
    sso_domain: str | None
    is_active: bool


class TokenUsageBucket(TypedDict):
    total_tokens: int
    cache_read_tokens: int
    cache_creation_tokens: int
    cache_hit_rate: float


class TenantTokenUsageResponse(TypedDict):
    today: TokenUsageBucket
    month: TokenUsageBucket
    total: TokenUsageBucket


class TenantAssignmentResponse(TypedDict):
    status: Literal["ok"]
    user_id: str
    tenant_id: str
    role: UserRole


class TenantDeletedResponse(TypedDict):
    status: Literal["deleted"]
    fallback_tenant_id: str | None
