"""OpenClaw upgrade notice and run records."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, NotRequired, TypedDict
from uuid import UUID

from pydantic import TypeAdapter

from app.core.json_types import (
    datetime_from_row,
    int_from_row,
    str_from_row,
    uuid_from_row,
    uuid_from_row_opt,
)

type RunState = Literal["pending", "cutover", "succeeded", "failed"]
ACTIVE_RUN_STATES: tuple[RunState, ...] = ("pending", "cutover")


class RunJournal(TypedDict):
    """Everything recovery needs to undo a cutover, written before each mutation."""

    target_image_id: NotRequired[str]
    old_container_id: NotRequired[str]
    old_image_id: NotRequired[str]
    old_port: NotRequired[int]
    canonical_name: NotRequired[str]
    parked_name: NotRequired[str]
    snapshot_dir: NotRequired[str]
    new_container_id: NotRequired[str]


_JOURNAL = TypeAdapter(RunJournal)


def parse_journal(value: object) -> RunJournal:
    return _JOURNAL.validate_python(value if isinstance(value, dict) else {})


@dataclass(frozen=True, slots=True)
class OpenClawUpdateNoticeRecord:
    id: UUID
    request_id: UUID
    title: str
    body: str
    mandatory: bool
    recipients: int
    created_at: datetime
    created_by: UUID | None = None

    @classmethod
    def from_row(cls, row: Mapping[str, object]) -> OpenClawUpdateNoticeRecord:
        created = datetime_from_row(row.get("created_at"))
        if created is None:
            raise ValueError("notice row has no created_at")
        return cls(
            id=uuid_from_row(row["id"]),
            request_id=uuid_from_row(row["request_id"]),
            title=str_from_row(row["title"]),
            body=str_from_row(row["body"]),
            mandatory=bool(row["mandatory"]),
            recipients=int_from_row(row.get("recipients")),
            created_at=created,
            created_by=uuid_from_row_opt(row.get("created_by")),
        )


@dataclass(frozen=True, slots=True)
class OpenClawUpdateRunRecord:
    id: UUID
    notice_id: UUID
    agent_id: UUID
    state: RunState
    failure_code: str | None = None
    requested_by: UUID | None = None
    journal: RunJournal = field(default_factory=lambda: RunJournal())
    created_at: datetime | None = None

    @classmethod
    def from_row(cls, row: Mapping[str, object]) -> OpenClawUpdateRunRecord:
        state = str_from_row(row["state"])
        if state not in ("pending", "cutover", "succeeded", "failed"):
            raise ValueError(f"unknown run state {state!r}")
        return cls(
            id=uuid_from_row(row["id"]),
            notice_id=uuid_from_row(row["notice_id"]),
            agent_id=uuid_from_row(row["agent_id"]),
            state=state,
            failure_code=str_from_row(row.get("failure_code")) or None,
            requested_by=uuid_from_row_opt(row.get("requested_by")),
            journal=parse_journal(row.get("journal")),
            created_at=datetime_from_row(row.get("created_at")),
        )

    @property
    def active(self) -> bool:
        return self.state in ACTIVE_RUN_STATES
