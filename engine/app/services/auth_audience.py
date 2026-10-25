"""Route-selected auth audiences and member eligibility policy."""

from enum import StrEnum
from typing import assert_never

from fastapi import HTTPException

from app.core.domain.users import UserRole
from app.dao import identity_dao, user_dao
from app.records.identity import IdentityRecord
from app.records.user import UserRecord


class AuthAudience(StrEnum):
    SHARED = "shared"
    MEMBER = "member"


def require_member_identity(identity: IdentityRecord) -> None:
    if identity.is_platform_admin:
        reject_member_audience()


def require_member_user(user: UserRecord) -> None:
    if user.identity is not None:
        require_member_identity(user.identity)
    match user.role:
        case UserRole.ORG_ADMIN | UserRole.AGENT_ADMIN | UserRole.MEMBER:
            return
        case UserRole.PLATFORM_ADMIN:
            reject_member_audience()
        case unreachable:
            assert_never(unreachable)


async def require_member_account(user: UserRecord) -> None:
    require_member_user(user)
    if user.identity_id is not None:
        identity = await identity_dao.get(user.identity_id)
        if identity is None:
            raise HTTPException(status_code=403, detail="Identity not found")
        require_member_identity(identity)
        for membership in await user_dao.get_by_identity_id(identity.id):
            require_member_user(membership)


def reject_member_audience() -> None:
    raise HTTPException(
        status_code=403,
        detail={
            "code": "member_audience_rejected",
            "message": "Platform operators sign in on the admin console.",
        },
    )
