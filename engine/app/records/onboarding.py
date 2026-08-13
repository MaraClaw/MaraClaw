"""User-tenant onboarding records."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID


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
    def from_row(cls, row: dict[str, Any]) -> UserTenantOnboardingRecord:
        return cls(
            id=row["id"],
            user_id=row["user_id"],
            tenant_id=row["tenant_id"],
            status=row.get("status") or "in_progress",
            current_step=row.get("current_step") or "assistant",
            entry_mode=row.get("entry_mode") or "create",
            personal_assistant_agent_id=row.get("personal_assistant_agent_id"),
            started_at=row.get("started_at"),
            completed_at=row.get("completed_at"),
            updated_at=row.get("updated_at"),
        )
