"""Agent Credentials CRUD API routes.

Provides endpoints for managing encrypted session cookies
per agent. Sensitive fields (cookies_json) are encrypted at rest
using AES-256-CBC and are NEVER returned in API responses.
"""

import json
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, status
from typing_extensions import TypedDict

from app.api.agent_credential_validation import CredentialSafeRoute
from app.config import get_settings
from app.core.json_types import json_as_str, json_loads_value, object_mapping_from
from app.core.permissions import check_agent_access
from app.core.security import encrypt_data, get_current_user
from app.dao.agent_credential_dao import (
    CredentialChanges,
    CredentialRevisionConflictError,
    agent_credential_dao,
    revision_conflict_detail,
)
from app.records.agent_credential import AgentCredentialRecord, CredentialRevision
from app.records.user import UserRecord
from app.schemas.agent_credential import (
    AgentCredentialCreate,
    AgentCredentialUpdate,
)

router = APIRouter(prefix="/agents/{agent_id}/credentials", tags=["agent-credentials"], route_class=CredentialSafeRoute)


class AgentCredentialPayload(TypedDict):
    id: uuid.UUID
    agent_id: uuid.UUID
    revision: int
    credential_type: str
    platform: str
    display_name: str
    status: str
    cookies_updated_at: datetime | None
    last_login_at: datetime | None
    last_injected_at: datetime | None
    has_cookies: bool
    created_at: datetime | None
    updated_at: datetime | None


def _to_response(cred: AgentCredentialRecord) -> AgentCredentialPayload:
    """Convert a credential record to a safe response dict.

    NEVER exposes cookies_json. Uses has_cookies as a presence flag instead.
    """
    return {
        "id": cred.id,
        "agent_id": cred.agent_id,
        "revision": cred.revision,
        "credential_type": cred.credential_type,
        "platform": cred.platform,
        "display_name": cred.display_name or "",
        "status": cred.status,
        "cookies_updated_at": cred.cookies_updated_at,
        "last_login_at": cred.last_login_at,
        "last_injected_at": cred.last_injected_at,
        "has_cookies": bool(cred.cookies_json),
        "created_at": cred.created_at,
        "updated_at": cred.updated_at,
    }


@router.get("/", response_model=None)
async def list_credentials(
    agent_id: uuid.UUID, current_user: UserRecord = Depends(get_current_user)
) -> list[AgentCredentialPayload]:
    """List all credentials for an agent (sensitive data excluded)."""
    # Verify the user has manage-level access to this agent
    _agent, access_level = await check_agent_access(current_user, agent_id)
    if access_level != "manage" and current_user.role not in ("platform_admin", "org_admin"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Manage access required to view credentials",
        )

    credentials = await agent_credential_dao.list_for_agent(agent_id)
    return [_to_response(c) for c in credentials]


@router.post("/", status_code=status.HTTP_201_CREATED, response_model=None)
async def create_credential(
    agent_id: uuid.UUID, data: AgentCredentialCreate, current_user: UserRecord = Depends(get_current_user)
) -> AgentCredentialPayload:
    """Create a new credential for an agent.

    Sensitive fields (cookies_json) are encrypted before storage.
    """
    _agent, access_level = await check_agent_access(current_user, agent_id)
    if access_level != "manage" and current_user.role not in ("platform_admin", "org_admin"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Manage access required to create credentials",
        )

    settings = get_settings()

    # Validate cookies_json format if provided
    if data.cookies_json not in (None, ""):
        try:
            parsed = json_loads_value(data.cookies_json)
            if not isinstance(parsed, list):
                raise TypeError("cookies_json must be a JSON array")
        except (json.JSONDecodeError, ValueError, TypeError) as e:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid cookies_json format: expected a JSON array",
            ) from e

    obj_in: dict[str, object] = {
        "agent_id": agent_id,
        "credential_type": data.credential_type,
        "platform": data.platform,
        "display_name": data.display_name or "",
        "status": "active",
    }

    # Encrypt sensitive fields
    if data.cookies_json not in (None, ""):
        obj_in["cookies_json"] = encrypt_data(data.cookies_json, settings.SECRET_KEY)
        obj_in["cookies_updated_at"] = datetime.now(UTC)

    cred = await agent_credential_dao.create(obj_in=obj_in)
    return _to_response(cred)


@router.put("/{credential_id}", response_model=None)
async def update_credential(
    agent_id: uuid.UUID,
    credential_id: uuid.UUID,
    data: AgentCredentialUpdate,
    current_user: UserRecord = Depends(get_current_user),
) -> AgentCredentialPayload:
    """Update an existing credential.

    Only provided fields are updated. Sensitive fields are re-encrypted.
    expected_revision must still be current; accepted edits advance it once
    and clear injection evidence. New cookies reset status to 'active'.
    """
    _agent, access_level = await check_agent_access(current_user, agent_id)
    if access_level != "manage" and current_user.role not in ("platform_admin", "org_admin"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Manage access required to update credentials",
        )

    cred = await agent_credential_dao.get_for_agent(credential_id, agent_id)
    if not cred:
        raise HTTPException(status_code=404, detail="Credential not found")

    settings = get_settings()
    update_data = object_mapping_from(data.model_dump(exclude_unset=True))
    obj_in: CredentialChanges = {}

    # Handle plaintext fields
    if "credential_type" in update_data:
        obj_in["credential_type"] = data.credential_type
    if "platform" in update_data:
        obj_in["platform"] = data.platform
    if "display_name" in update_data:
        obj_in["display_name"] = data.display_name
    if "status" in update_data:
        obj_in["status"] = data.status

    if "cookies_json" in update_data:
        if update_data["cookies_json"]:
            # Validate JSON format
            try:
                cookies_json = json_as_str(update_data["cookies_json"])
                if cookies_json is None:
                    raise ValueError("cookies_json must be a JSON array")
                parsed = json_loads_value(cookies_json)
                if not isinstance(parsed, list):
                    raise TypeError("cookies_json must be a JSON array")
                update_data["cookies_json"] = cookies_json
            except (json.JSONDecodeError, ValueError, TypeError) as e:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Invalid cookies_json format: expected a JSON array",
                ) from e
            obj_in["cookies_json"] = encrypt_data(cookies_json, settings.SECRET_KEY)
            obj_in["cookies_updated_at"] = datetime.now(UTC)
            # Reset status to active when cookies are updated
            obj_in["status"] = "active"
        else:
            obj_in["cookies_json"] = None
            obj_in["cookies_updated_at"] = None

    try:
        updated = await agent_credential_dao.edit(
            CredentialRevision(credential_id, agent_id, data.expected_revision), obj_in
        )
    except CredentialRevisionConflictError as error:
        raise HTTPException(status_code=409, detail=revision_conflict_detail()) from error
    return _to_response(updated)


@router.delete("/{credential_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_credential(
    agent_id: uuid.UUID, credential_id: uuid.UUID, current_user: UserRecord = Depends(get_current_user)
) -> None:
    """Delete a credential."""
    _agent, access_level = await check_agent_access(current_user, agent_id)
    if access_level != "manage" and current_user.role not in ("platform_admin", "org_admin"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Manage access required to delete credentials",
        )

    cred = await agent_credential_dao.get_for_agent(credential_id, agent_id)
    if not cred:
        raise HTTPException(status_code=404, detail="Credential not found")

    _ = await agent_credential_dao.delete(record_id=cred.id)
