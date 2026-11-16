"""Immutable, credential-free references to verified channel bytes."""

from pathlib import PurePosixPath
from typing import ClassVar, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.records.chat_attachment import ChatAttachment


class StoredFile(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid", strict=True)

    storage_key: str = Field(min_length=1, max_length=1200)
    path: str = Field(min_length=1, max_length=1024)
    name: str = Field(min_length=1, max_length=255)
    size: int = Field(ge=0, le=50 * 1024 * 1024)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    kind: Literal["image", "file"]

    @model_validator(mode="after")
    def contained_reference(self) -> StoredFile:
        ref = ChatAttachment(path=self.path, name=self.name)
        owner, separator, relative = self.storage_key.partition("/")
        if (
            not separator
            or str(UUID(owner)) != owner
            or relative != ref.path
            or not ref.path.startswith("workspace/uploads/zalo/")
            or PurePosixPath(ref.path).name != ref.name
            or (self.kind == "image" and self.size > 10 * 1024 * 1024)
        ):
            raise ValueError("Invalid stored channel file reference")
        return self
