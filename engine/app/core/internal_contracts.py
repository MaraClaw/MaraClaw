"""Internal callable and token claims contracts without application setup."""

from datetime import datetime
from typing import TYPE_CHECKING, Protocol

from typing_extensions import TypedDict

from app.core.json_types import JsonValue

if TYPE_CHECKING:
    from app.records.user import UserRecord


class RoleChecker(Protocol):
    async def __call__(self, current_user: UserRecord) -> UserRecord: ...


class Sink(Protocol):
    def __call__(self, chunk: str, /) -> object: ...


class AccessTokenClaims(TypedDict, extra_items=JsonValue):
    sub: str
    role: str
    exp: datetime | int
    gen: int


class AccessTokenPayload(TypedDict, extra_items=JsonValue):
    sub: str
    role: str
    exp: int
    gen: int
