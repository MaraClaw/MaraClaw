"""gogcli integration endpoints for agent containers."""

import uuid
from typing import Annotated, ClassVar, Final

from anyio.to_thread import run_sync
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, SecretStr

from app.core.logging import logger
from app.core.permissions import check_agent_access
from app.core.security import get_current_user
from app.records.agent import AgentRecord
from app.records.user import UserRecord
from app.services.agent_manager import agent_manager
from app.services.gogcli_oauth import (
    GogcliDockerClient,
    get_gogcli_auth_status as read_gogcli_auth_status,
    start_gogcli_auth as start_gogcli_auth_handoff,
)
from app.services.gogcli_persistence import (
    capture_authenticated_gogcli_state,
    mark_gogcli_needs_reauth_if_snapshot_exists,
    upsert_gogcli_keyring_password,
)
from app.services.gogcli_runtime import write_gogcli_keyring_secret

router = APIRouter(prefix="/agents/{agent_id}/gogcli", tags=["gogcli"])
GOGCLI_ADMIN_ROLES: Final = frozenset({"platform_admin", "org_admin"})


class GogcliKeyringSecretRequest(BaseModel):
    """Per-agent gogcli file-keyring secret request."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)

    password: SecretStr


class GogcliAuthStartRequest(BaseModel):
    """gogcli OAuth start request."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)

    account_email: str


class GogcliAuthStartResponse(BaseModel):
    """Safe gogcli OAuth start response."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)

    auth_url: str
    detail: str


class GogcliAuthStatusResponse(BaseModel):
    """Safe gogcli OAuth status response."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)

    authenticated: bool
    account_hint: str | None
    detail: str


async def _get_gogcli_manage_agent(db: object | None, current_user: UserRecord, agent_id: uuid.UUID) -> AgentRecord:
    """Return a gogcli-enabled agent after enforcing manage/admin access."""
    agent, access_level = await check_agent_access(current_user, agent_id)
    if access_level == "manage" or current_user.role in GOGCLI_ADMIN_ROLES:
        if agent.gogcli_enabled:
            return agent
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="gogcli is not enabled for this agent")
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Manage access required")


def _require_gogcli_container(agent: AgentRecord) -> None:
    """Reject OAuth operations until the agent already has a running container."""
    if not agent.container_id:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Agent container is not running")


def _gogcli_docker_client() -> GogcliDockerClient | None:
    docker_client = agent_manager.docker_client
    if docker_client is None:
        return None
    return docker_client.container


@router.post("/keyring-secret", status_code=status.HTTP_204_NO_CONTENT)
async def set_gogcli_keyring_secret(
    agent_id: uuid.UUID,
    data: GogcliKeyringSecretRequest,
    current_user: Annotated[UserRecord, Depends(get_current_user)],
    db: object | None = None,
) -> None:
    """Store or rotate the file-keyring password for a gogcli-enabled agent."""
    agent = await _get_gogcli_manage_agent(db, current_user, agent_id)

    password = data.password.get_secret_value()
    _ = await run_sync(write_gogcli_keyring_secret, agent.id, password)
    _ = await upsert_gogcli_keyring_password(db, agent.id, password)
    logger.info(f"gogcli keyring secret updated for agent {agent.id}")


@router.post("/auth/start", response_model=GogcliAuthStartResponse)
async def start_gogcli_auth(
    agent_id: uuid.UUID,
    data: GogcliAuthStartRequest,
    current_user: Annotated[UserRecord, Depends(get_current_user)],
    db: object | None = None,
) -> GogcliAuthStartResponse:
    """Start a safe gogcli OAuth handoff for an already-running agent container."""
    agent = await _get_gogcli_manage_agent(db, current_user, agent_id)
    _require_gogcli_container(agent)

    result = await start_gogcli_auth_handoff(_gogcli_docker_client(), agent, data.account_email)
    if not result.started or result.auth_url is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Authentication could not be started")
    return GogcliAuthStartResponse(auth_url=result.auth_url, detail=result.detail)


@router.get("/auth/status", response_model=GogcliAuthStatusResponse)
async def get_gogcli_auth_status(
    agent_id: uuid.UUID,
    current_user: Annotated[UserRecord, Depends(get_current_user)],
    db: object | None = None,
) -> GogcliAuthStatusResponse:
    """Return safe gogcli OAuth status fields for an already-running agent container."""
    agent = await _get_gogcli_manage_agent(db, current_user, agent_id)
    _require_gogcli_container(agent)

    result = await read_gogcli_auth_status(_gogcli_docker_client(), agent)
    detail = result.detail
    if result.authenticated:
        _ = await capture_authenticated_gogcli_state(db, agent.id, agent_manager._agent_dir(agent.id), result)
    elif await mark_gogcli_needs_reauth_if_snapshot_exists(db, agent.id):
        detail = "Needs re-authentication"
    return GogcliAuthStatusResponse(
        authenticated=result.authenticated,
        account_hint=result.account_hint,
        detail=detail,
    )
