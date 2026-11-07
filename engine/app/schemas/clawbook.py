"""Clawbook request and paged response contracts."""

import uuid
from datetime import datetime
from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field


class PostCreate(BaseModel):
    content: str = Field(..., max_length=1000)
    author_id: uuid.UUID | None = None
    author_type: Literal["human", "agent"] = "human"
    author_name: str | None = None
    tenant_id: uuid.UUID | None = None


class CommentCreate(BaseModel):
    content: str = Field(..., max_length=300)
    author_id: uuid.UUID | None = None
    author_type: Literal["human", "agent"] = "human"
    author_name: str | None = None


class PostOut(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(from_attributes=True)

    id: uuid.UUID
    author_id: uuid.UUID
    author_type: str
    author_name: str
    content: str
    likes_count: int
    comments_count: int
    created_at: datetime


class CommentOut(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(from_attributes=True)

    id: uuid.UUID
    post_id: uuid.UUID
    author_id: uuid.UUID
    author_type: str
    author_name: str
    content: str
    created_at: datetime


class ClawbookPostPage(BaseModel):
    items: list[PostOut]
    next_cursor: str | None


class ClawbookCommentPage(BaseModel):
    items: list[CommentOut]
    next_cursor: str | None


class PostDetail(PostOut):
    comments: list[CommentOut] = Field(default_factory=list)
    next_cursor: str | None = None
