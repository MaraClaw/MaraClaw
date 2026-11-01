from collections.abc import Awaitable, Callable
from typing import Literal

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials

from app.core.security import load_user_from_access_token, require_role, security
from app.records.user import UserRecord


def require_admin_audit_role(
    role: Literal["platform_admin", "org_admin"],
) -> Callable[[HTTPAuthorizationCredentials], Awaitable[UserRecord]]:
    check_role = require_role(role)

    async def authorize(credentials: HTTPAuthorizationCredentials = Depends(security)) -> UserRecord:
        principal = await load_user_from_access_token(credentials.credentials, fresh=True)
        return await check_role(principal)

    return authorize
