"""Plaza (Agent Square) records."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from app.core.json_types import datetime_from_row, int_from_row, str_from_row, uuid_from_row, uuid_from_row_opt


@dataclass(slots=True)
class PlazaPostRecord:
    """A post in the Agent Plaza social feed."""

    id: UUID
    author_id: UUID
    author_type: str
    author_name: str
    content: str
    tenant_id: UUID | None = None
    likes_count: int = 0
    comments_count: int = 0
    created_at: datetime | None = None

    @classmethod
    def from_row(cls, row: Mapping[str, object]) -> PlazaPostRecord:
        return cls(
            id=uuid_from_row(row["id"]),
            author_id=uuid_from_row(row["author_id"]),
            author_type=str_from_row(row["author_type"]),
            author_name=str_from_row(row["author_name"]),
            content=str_from_row(row["content"]),
            tenant_id=uuid_from_row_opt(row.get("tenant_id")),
            likes_count=int_from_row(row.get("likes_count")),
            comments_count=int_from_row(row.get("comments_count")),
            created_at=datetime_from_row(row.get("created_at")),
        )


@dataclass(slots=True)
class PlazaCommentRecord:
    """A comment on a plaza post."""

    id: UUID
    post_id: UUID
    author_id: UUID
    author_type: str
    author_name: str
    content: str
    created_at: datetime | None = None

    @classmethod
    def from_row(cls, row: Mapping[str, object]) -> PlazaCommentRecord:
        return cls(
            id=uuid_from_row(row["id"]),
            post_id=uuid_from_row(row["post_id"]),
            author_id=uuid_from_row(row["author_id"]),
            author_type=str_from_row(row["author_type"]),
            author_name=str_from_row(row["author_name"]),
            content=str_from_row(row["content"]),
            created_at=datetime_from_row(row.get("created_at")),
        )


@dataclass(slots=True)
class PlazaLikeRecord:
    """A like on a plaza post (prevents duplicate likes)."""

    id: UUID
    post_id: UUID
    author_id: UUID
    author_type: str
    created_at: datetime | None = None

    @classmethod
    def from_row(cls, row: Mapping[str, object]) -> PlazaLikeRecord:
        return cls(
            id=uuid_from_row(row["id"]),
            post_id=uuid_from_row(row["post_id"]),
            author_id=uuid_from_row(row["author_id"]),
            author_type=str_from_row(row["author_type"]),
            created_at=datetime_from_row(row.get("created_at")),
        )
