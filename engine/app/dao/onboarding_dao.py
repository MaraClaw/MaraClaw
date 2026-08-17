"""DAO for user_tenant_onboardings"""

from __future__ import annotations

from typing import ClassVar, final
from uuid import UUID, uuid4

from app.dao.base import BaseDAO
from app.records.onboarding import UserTenantOnboardingRecord

_COLUMNS = (
    "id",
    "user_id",
    "tenant_id",
    "status",
    "current_step",
    "entry_mode",
    "personal_assistant_agent_id",
    "started_at",
    "completed_at",
    "updated_at",
)


@final
class UserTenantOnboardingDAO(BaseDAO[UserTenantOnboardingRecord]):
    table: ClassVar[str] = "user_tenant_onboardings"
    columns: ClassVar[tuple[str, ...]] = _COLUMNS
    record_factory = staticmethod(UserTenantOnboardingRecord.from_row)

    async def get_for_user_tenant(self, user_id: UUID, tenant_id: UUID) -> UserTenantOnboardingRecord | None:
        async with self.session() as db:
            row = await db.fetchone(
                f"SELECT {self._select_list()} FROM user_tenant_onboardings "
                + "WHERE user_id = %(user_id)s AND tenant_id = %(tenant_id)s LIMIT 1",
                {"user_id": user_id, "tenant_id": tenant_id},
            )
            return UserTenantOnboardingRecord.from_row(row) if row else None

    async def insert_ignore(
        self,
        *,
        user_id: UUID,
        tenant_id: UUID,
        entry_mode: str,
        current_step: str = "assistant",
        status: str = "in_progress",
    ) -> None:
        async with self.session() as db:
            await db.execute(
                "INSERT INTO user_tenant_onboardings ("
                + "id, user_id, tenant_id, entry_mode, current_step, status"
                + ") VALUES ("
                + "%(id)s, %(user_id)s, %(tenant_id)s, %(entry_mode)s, %(current_step)s, %(status)s"
                + ") ON CONFLICT ON CONSTRAINT uq_user_tenant_onboarding DO NOTHING",
                {
                    "id": uuid4(),
                    "user_id": user_id,
                    "tenant_id": tenant_id,
                    "entry_mode": entry_mode,
                    "current_step": current_step,
                    "status": status,
                },
            )


user_tenant_onboarding_dao = UserTenantOnboardingDAO()
