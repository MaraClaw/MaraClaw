"""Public execution history and its context-bound pagination position."""

import base64
import re
from typing import Annotated, ClassVar
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from app.core.domain.triggers import TriggerExecutionStatus


class TriggerExecutionHistoryItem(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, from_attributes=True)

    id: UUID
    trigger_id: UUID
    agent_id: UUID
    source: str
    status: TriggerExecutionStatus
    scheduled_at: AwareDatetime
    started_at: AwareDatetime | None
    finished_at: AwareDatetime | None
    created_at: AwareDatetime


class TriggerExecutionHistoryPage(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)

    items: list[TriggerExecutionHistoryItem]
    next_cursor: str | None


class TriggerExecutionCursor(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid", strict=True)

    version: Annotated[int, Field(strict=True, ge=1, le=1)]
    agent_id: UUID
    trigger_id: UUID
    created_at: AwareDatetime
    id: UUID

    @classmethod
    def decode(cls, token: str, agent_id: UUID, trigger_id: UUID) -> TriggerExecutionCursor:
        if len(token) > 2048 or re.fullmatch(r"[A-Za-z0-9_-]+", token) is None:
            raise ValueError("Invalid cursor")
        payload = base64.b64decode(token + "=" * (-len(token) % 4), altchars=b"-_", validate=True)
        position = cls.model_validate_json(payload)
        if position.agent_id != agent_id or position.trigger_id != trigger_id:
            raise ValueError("Invalid cursor")
        return position

    def encode(self) -> str:
        return base64.urlsafe_b64encode(self.model_dump_json().encode()).decode().rstrip("=")
