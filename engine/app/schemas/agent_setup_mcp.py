from typing import Annotated, final

from pydantic import BaseModel, ConfigDict, Field

from app.records.agent_setup_mcp import MCPWarningCode, MCPWorkState


@final
class AgentSetupMCPStatus(BaseModel):
    model_config = ConfigDict(frozen=True)

    server_id: str
    state: MCPWorkState
    warning_code: MCPWarningCode | None
    attempts: int = Field(ge=0)
    retryable: bool


@final
class AgentSetupMCPRetry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    server_id: Annotated[str, Field(min_length=1, max_length=512, pattern=r"^\S+$")]
