"""Skill registry records."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, final
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.json_types import datetime_from_row, str_from_row, uuid_from_row, uuid_from_row_opt


@final
class SkillSource(BaseModel):
    """Credential-free download provenance, never a provider authenticity claim."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: Literal["clawhub"] = "clawhub"
    slug: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
    owner: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")
    requested_version: str | None = None
    archive_source: str

    @field_validator("archive_source")
    @classmethod
    def public_archive_source(cls, value: str) -> str:
        url = urlsplit(value)
        hostname = url.hostname
        if url.scheme not in ("http", "https") or hostname is None or hostname == "":
            raise ValueError("Archive source must be an HTTP URL")
        host = f"[{hostname}]" if ":" in hostname else hostname
        authority = f"{host}:{url.port}" if url.port is not None else host
        return urlunsplit((url.scheme, authority, "", "", ""))


@dataclass(slots=True)
class SkillFileRecord:
    """A file within a skill folder."""

    id: UUID
    skill_id: UUID
    path: str
    content: str = ""

    @classmethod
    def from_row(cls, row: Mapping[str, object]) -> SkillFileRecord:
        return cls(
            id=uuid_from_row(row["id"]),
            skill_id=uuid_from_row(row["skill_id"]),
            path=str_from_row(row["path"]),
            content=str_from_row(row.get("content")),
        )


@dataclass(slots=True)
class SkillRecord:
    """Globally registered skill definition."""

    id: UUID
    name: str
    folder_name: str
    tenant_id: UUID | None = None
    description: str = ""
    category: str = "general"
    icon: str = "📋"
    is_builtin: bool = False
    is_default: bool = False
    source_metadata: SkillSource | None = None
    imported_fingerprint: str | None = None
    created_at: datetime | None = None
    files: list[SkillFileRecord] = field(default_factory=list[SkillFileRecord])

    @classmethod
    def from_row(cls, row: Mapping[str, object], *, files: list[SkillFileRecord] | None = None) -> SkillRecord:
        return cls(
            id=uuid_from_row(row["id"]),
            name=str_from_row(row["name"]),
            folder_name=str_from_row(row["folder_name"]),
            tenant_id=uuid_from_row_opt(row.get("tenant_id")),
            description=str_from_row(row.get("description")),
            category=str_from_row(row.get("category"), "general") or "general",
            icon=str_from_row(row.get("icon"), "📋") or "📋",
            is_builtin=bool(row.get("is_builtin", False)),
            is_default=bool(row.get("is_default", False)),
            source_metadata=(
                SkillSource.model_validate(row["source_metadata"]) if row.get("source_metadata") is not None else None
            ),
            imported_fingerprint=(
                str_from_row(row["imported_fingerprint"]) if row.get("imported_fingerprint") is not None else None
            ),
            created_at=datetime_from_row(row.get("created_at")),
            files=list(files or []),
        )
