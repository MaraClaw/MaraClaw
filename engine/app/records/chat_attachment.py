"""Untrusted chat file references, with no client-supplied file metadata."""

from pathlib import PurePosixPath, PureWindowsPath
from typing import ClassVar, TypedDict
from unicodedata import category

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator


class AttachmentRef(TypedDict):
    path: str
    name: str


class ChatAttachment(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid", strict=True)

    path: str = Field(min_length=1, max_length=1024)
    name: str = Field(min_length=1, max_length=255)

    @field_validator("path")
    @classmethod
    def canonical_agent_path(cls, value: str) -> str:
        relative = value.strip()
        path = PurePosixPath(relative)
        if (
            path.is_absolute()
            or PureWindowsPath(relative).drive
            or PurePosixPath(value).as_posix() != value
            or ".." in path.parts
            or path.as_posix() == "."
            or "\\" in value
            or relative.startswith("enterprise_info")
            or any(category(char) == "Cc" for char in value)
        ):
            raise ValueError("Expected a canonical agent-relative file path")
        return value

    @field_validator("name")
    @classmethod
    def display_filename(cls, value: str) -> str:
        if value in (".", "..") or "/" in value or "\\" in value or any(category(char) == "Cc" for char in value):
            raise ValueError("Expected a display filename")
        return value

    def as_dict(self) -> AttachmentRef:
        return {"path": self.path, "name": self.name}


chat_attachments_adapter = TypeAdapter(tuple[ChatAttachment, ...])
