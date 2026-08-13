"""Plaza (Agent Square) records."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID


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
    def from_row(cls, row: dict[str, Any]) -> PlazaPostRecord:
        return cls(
            id=row["id"],
            author_id=row["author_id"],
            author_type=row["author_type"],
            author_name=row["author_name"],
            content=row["content"],
            tenant_id=row.get("tenant_id"),
            likes_count=int(row.get("likes_count") or 0),
            comments_count=int(row.get("comments_count") or 0),
            created_at=row.get("created_at"),
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
    def from_row(cls, row: dict[str, Any]) -> PlazaCommentRecord:
        return cls(
            id=row["id"],
            post_id=row["post_id"],
            author_id=row["author_id"],
            author_type=row["author_type"],
            author_name=row["author_name"],
            content=row["content"],
            created_at=row.get("created_at"),
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
    def from_row(cls, row: dict[str, Any]) -> PlazaLikeRecord:
        return cls(
            id=row["id"],
            post_id=row["post_id"],
            author_id=row["author_id"],
            author_type=row["author_type"],
            created_at=row.get("created_at"),
        )
