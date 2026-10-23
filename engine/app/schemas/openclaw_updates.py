"""HTTP contract for global OpenClaw upgrade notices and per-agent upgrade runs."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field


class OpenClawNoticeOut(BaseModel):
    id: UUID
    title: str
    body: str
    mandatory: bool
    created_at: datetime


class OpenClawRunOut(BaseModel):
    id: UUID
    state: Literal["pending", "cutover", "succeeded", "failed"]
    failure_code: str | None


class AgentUpdateOut(BaseModel):
    notice: OpenClawNoticeOut | None
    run: OpenClawRunOut | None
    required: bool
    can_upgrade: bool


class PublishNoticeIn(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    body: str = Field(default="", max_length=1000)
    mandatory: bool
    request_id: UUID


class PublishNoticeOut(BaseModel):
    notice: OpenClawNoticeOut
    recipients: int


class NoticeAgentOut(BaseModel):
    agent_id: UUID
    name: str
    status: str
    update: AgentUpdateOut


class NoticeDetailOut(BaseModel):
    notice: OpenClawNoticeOut
    agents: list[NoticeAgentOut]


class RequestUpgradeIn(BaseModel):
    notice_id: UUID
