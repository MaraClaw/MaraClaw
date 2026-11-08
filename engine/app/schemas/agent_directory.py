"""Member agent directory page and nullable SQL position."""

from dataclasses import dataclass
from datetime import datetime
from typing import ClassVar
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from app.schemas.schemas import AgentOut


@dataclass(frozen=True, slots=True)
class AgentDirectoryPosition:
    created_at: datetime | None
    id: UUID


class AgentDirectoryOut(AgentOut[datetime | None]):
    pass


class AgentPageOut(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)

    items: list[AgentDirectoryOut]
    next_cursor: str | None
    has_more: bool
