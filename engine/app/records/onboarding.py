"""User-tenant onboarding records."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from app.core.json_types import datetime_from_row, str_from_row, uuid_from_row, uuid_from_row_opt


@dataclass(slots=True)
class UserTenantOnboardingRecord:
    """Tracks onboarding for one user in one company."""

    id: UUID
    user_id: UUID
    tenant_id: UUID
    status: str = "in_progress"
    current_step: str = "assistant"
    entry_mode: str = "create"
    personal_assistant_agent_id: UUID | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    updated_at: datetime | None = None

    @classmethod
    def from_row(cls, row: Mapping[str, object]) -> UserTenantOnboardingRecord:
        return cls(
            id=uuid_from_row(row["id"]),
            user_id=uuid_from_row(row["user_id"]),
            tenant_id=uuid_from_row(row["tenant_id"]),
            status=str_from_row(row.get("status"), "in_progress") or "in_progress",
            current_step=str_from_row(row.get("current_step"), "assistant") or "assistant",
            entry_mode=str_from_row(row.get("entry_mode"), "create") or "create",
            personal_assistant_agent_id=uuid_from_row_opt(row.get("personal_assistant_agent_id")),
            started_at=datetime_from_row(row.get("started_at")),
            completed_at=datetime_from_row(row.get("completed_at")),
            updated_at=datetime_from_row(row.get("updated_at")),
        )
