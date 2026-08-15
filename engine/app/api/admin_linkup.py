"""Platform-admin add/list/remove for stored Linkup API keys."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, field_validator

from app.core.security import require_role
from app.records.user import UserRecord
from app.services.admin_audit import field_change, write_admin_audit
from app.services.linkup.keys import (
    DuplicateLinkupKeyError,
    LinkupKeyNotFoundError,
    add_key,
    list_keys,
    public_key_view,
    remove_key,
)

router = APIRouter(prefix="/admin", tags=["admin"])


class LinkupKeyCreateRequest(BaseModel):
    label: str = Field(min_length=1, max_length=200)
    api_key: str = Field(min_length=1, max_length=512)

    @field_validator("label", "api_key", mode="before")
    @classmethod
    def strip_required(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value


class LinkupKeyOut(BaseModel):
    id: UUID
    label: str
    fingerprint: str
    position: int
    status: str
    exhausted_until: datetime | None = None
    last_used_at: datetime | None = None
    created_at: datetime | None = None


async def get_client_ip(request: Request) -> str | None:
    if request.client is None:
        return None
    return request.client.host


def _to_out(view: dict[str, object]) -> LinkupKeyOut:
    return LinkupKeyOut.model_validate(view)


@router.get("/linkup-keys", response_model=list[LinkupKeyOut])
async def list_linkup_keys(
    current_user: UserRecord = Depends(require_role("platform_admin")),
) -> list[LinkupKeyOut]:
    del current_user
    return [_to_out(public_key_view(record)) for record in await list_keys()]


@router.post("/linkup-keys", response_model=LinkupKeyOut, status_code=201)
async def create_linkup_key(
    data: LinkupKeyCreateRequest,
    current_user: UserRecord = Depends(require_role("platform_admin")),
    client_ip: str | None = Depends(get_client_ip),
) -> LinkupKeyOut:
    try:
        record = await add_key(label=data.label, api_key=data.api_key)
    except DuplicateLinkupKeyError as exc:
        raise HTTPException(status_code=409, detail="This Linkup API key is already stored") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    await write_admin_audit(
        actor=current_user,
        action="linkup_key_add",
        target_type="linkup_api_key",
        target_id=record.id,
        changes={"fingerprint": field_change(None, record.key_fingerprint)},
        ip_address=client_ip,
    )
    return _to_out(public_key_view(record))


@router.delete("/linkup-keys/{key_id}", response_model=LinkupKeyOut)
async def delete_linkup_key(
    key_id: UUID,
    current_user: UserRecord = Depends(require_role("platform_admin")),
    client_ip: str | None = Depends(get_client_ip),
) -> LinkupKeyOut:
    try:
        record = await remove_key(key_id)
    except LinkupKeyNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Linkup API key not found") from exc

    await write_admin_audit(
        actor=current_user,
        action="linkup_key_remove",
        target_type="linkup_api_key",
        target_id=record.id,
        changes={"fingerprint": field_change(record.key_fingerprint, None)},
        ip_address=client_ip,
    )
    return _to_out(public_key_view(record))
