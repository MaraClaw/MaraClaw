from typing import ClassVar
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class OwnershipTransferRequest(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")

    from_user_id: UUID
    to_user_id: UUID
    agent_ids: list[UUID] = Field(min_length=1, max_length=100)


class OwnershipTransferResponse(BaseModel):
    transferred: list[str]
    cancelled_deletion_request_ids: list[str]
