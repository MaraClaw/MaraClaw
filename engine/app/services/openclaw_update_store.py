"""Persistence seam of the OpenClaw upgrade executor (Postgres in production, fakes in tests)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import UUID

from app.core.row_memo import memo_drop
from app.dao.agent_dao import agent_dao
from app.dao.gateway_message_dao import gateway_message_dao
from app.dao.openclaw_update_dao import openclaw_update_run_dao
from app.records.agent import AgentRecord
from app.records.openclaw_update import OpenClawUpdateRunRecord, RunJournal


class UpgradeStore(Protocol):
    async def list_active(self) -> list[OpenClawUpdateRunRecord]: ...
    async def get_run(self, run_id: UUID) -> OpenClawUpdateRunRecord | None: ...
    async def get_agent(self, agent_id: UUID) -> AgentRecord | None: ...
    async def begin_cutover(self, run_id: UUID, journal: RunJournal) -> bool: ...
    async def set_journal(self, run_id: UUID, journal: RunJournal) -> None: ...
    async def finish_failed(self, run_id: UUID, code: str) -> None: ...
    async def fail_pending(self, run_id: UUID, code: str) -> bool: ...
    async def publish_success(
        self,
        run_id: UUID,
        agent_id: UUID,
        *,
        old_container_id: str,
        new_container_id: str,
        port: int | None,
        image_id: str,
    ) -> bool: ...
    async def pin_image(self, agent_id: UUID, image_id: str) -> None: ...
    async def mark_rollback_failed(self, agent_id: UUID, image_id: str | None) -> None: ...
    async def requeue_inflight(self, agent_id: UUID) -> None: ...


class PostgresUpgradeStore:
    async def list_active(self) -> list[OpenClawUpdateRunRecord]:
        return list(await openclaw_update_run_dao.list_active())

    async def get_run(self, run_id: UUID) -> OpenClawUpdateRunRecord | None:
        return await openclaw_update_run_dao.get(run_id)

    async def get_agent(self, agent_id: UUID) -> AgentRecord | None:
        memo_drop("agent", agent_id)
        return await agent_dao.get(agent_id)

    async def begin_cutover(self, run_id: UUID, journal: RunJournal) -> bool:
        return await openclaw_update_run_dao.begin_cutover(run_id, journal)

    async def set_journal(self, run_id: UUID, journal: RunJournal) -> None:
        await openclaw_update_run_dao.set_journal(run_id, journal)

    async def finish_failed(self, run_id: UUID, code: str) -> None:
        await openclaw_update_run_dao.finish_failed(run_id, code)

    async def fail_pending(self, run_id: UUID, code: str) -> bool:
        return await openclaw_update_run_dao.fail_pending(run_id, code)

    async def publish_success(
        self,
        run_id: UUID,
        agent_id: UUID,
        *,
        old_container_id: str,
        new_container_id: str,
        port: int | None,
        image_id: str,
    ) -> bool:
        return await openclaw_update_run_dao.publish_success(
            run_id,
            agent_id,
            old_container_id=old_container_id,
            new_container_id=new_container_id,
            port=port,
            image_id=image_id,
        )

    async def pin_image(self, agent_id: UUID, image_id: str) -> None:
        await openclaw_update_run_dao.pin_image(agent_id, image_id)

    async def mark_rollback_failed(self, agent_id: UUID, image_id: str | None) -> None:
        if image_id is not None:
            await openclaw_update_run_dao.pin_image(agent_id, image_id)
        await openclaw_update_run_dao.mark_agent_error(agent_id)

    async def requeue_inflight(self, agent_id: UUID) -> None:
        """Every delivery claimed by the guest goes back to pending, the shared lane included.

        Callers invoke this only after the guest container is stopped: nothing can report the rows any more, and
        ``claim_shared`` refuses a lane that still holds a delivered row, so leaving it would strand the lane.
        """
        _ = await gateway_message_dao.requeue_stale_deliveries(
            agent_id, stale_before=datetime.now(UTC) + timedelta(days=1)
        )
