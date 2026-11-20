"""Request and response models for the agent-facing file share link endpoint."""

from datetime import datetime
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field

MAX_PATH_LENGTH = 1024


class FileShareLinkRequest(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True)

    path: str = Field(min_length=1, max_length=MAX_PATH_LENGTH)


class FileShareLinkResponse(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)

    url: str
    expires_at: datetime
    expires_in_seconds: int
    filename: str
    size_bytes: int
