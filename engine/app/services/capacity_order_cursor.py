"""Opaque keyset position in a capacity order list, bound to the view that issued it.

The position is the ``(created_at, id)`` of the last order a client saw, never an offset, so the next page seeks
strictly below it however the list changed in between. The view (one company's history, or the platform queue of one
status) travels inside the cursor and must match the request that redeems it.
"""

from base64 import b64decode, urlsafe_b64encode
from binascii import Error as Base64Error
from typing import ClassVar, Final, Self
from uuid import UUID

from fastapi import HTTPException
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from app.records.capacity_order import CapacityOrderRecord, CapacityOrderStatus

MAX_CURSOR_LENGTH: Final = 1024


def company_view(tenant_id: UUID) -> str:
    """The history of one company, as an organization admin or a platform admin naming it reads it."""
    return f"company:{tenant_id}"


def review_view(status: CapacityOrderStatus | None) -> str:
    """The platform review queue of one status (``None`` is every status)."""
    return f"review:{status.value if status is not None else 'all'}"


class CapacityOrderCursor(BaseModel):
    """Canonical base64url JSON ``{v, view, created_at, id}``; any other shape, version or view is invalid."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True)

    v: int = Field(strict=True, ge=1, le=1)
    view: str
    created_at: AwareDatetime
    id: UUID

    @classmethod
    def after(cls, order: CapacityOrderRecord, view: str) -> Self:
        """The position just past ``order`` in ``view``."""
        return cls(v=1, view=view, created_at=order.created_at, id=order.id)

    def encode(self) -> str:
        return urlsafe_b64encode(self.model_dump_json().encode()).decode().rstrip("=")

    @classmethod
    def decode(cls, value: str, view: str) -> Self:
        """The position in ``value``; a malformed, other-version or other-view cursor is a 400."""
        if not value or len(value) > MAX_CURSOR_LENGTH:
            raise HTTPException(400, "Invalid cursor")
        try:
            payload = b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
            position = cls.model_validate_json(payload, strict=True)
        except (Base64Error, ValueError) as exc:
            raise HTTPException(400, "Invalid cursor") from exc
        if urlsafe_b64encode(payload).decode().rstrip("=") != value or position.view != view:
            raise HTTPException(400, "Invalid cursor")
        return position
