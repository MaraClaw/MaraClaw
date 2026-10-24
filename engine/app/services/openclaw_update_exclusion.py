"""Check-lock-recheck exclusion between guest-visible changes and OpenClaw cutovers.

Cutover holds the exclusive per-agent workspace flock from stop to publication or
rollback, so a rolled-back snapshot can never discard a concurrent write.
Writers hold the shared lock; lifecycle changes hold the exclusive one.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Annotated
from uuid import UUID

from fastapi import Depends, HTTPException, status

from app.core.permissions import check_agent_access
from app.core.row_memo import memo_drop
from app.core.security import get_current_user
from app.dao.agent_dao import agent_dao
from app.records.agent import AgentRecord
from app.records.user import UserRecord
from app.services.agent_manager import agent_manager
from app.services.agent_setup_lock import setup_workspace_lock
from app.services.company_transfer_runtime_gate import assert_transfer_ready
from app.services.openclaw_update_gate import assert_no_active_run

GUEST_WRITE_LOCK_TIMEOUT_SECONDS = 1.0


@asynccontextmanager
async def guest_write_section(agent_id: UUID) -> AsyncGenerator[None]:
    """Shared section for code that changes files the guest can see."""
    await assert_no_active_run(agent_id)
    async with setup_workspace_lock(
        agent_manager._agent_dir(agent_id), shared=True, timeout_seconds=GUEST_WRITE_LOCK_TIMEOUT_SECONDS
    ):
        await assert_no_active_run(agent_id)
        await assert_transfer_ready(agent_id)
        yield


async def guest_write_exclusion(
    agent_id: UUID, current_user: Annotated[UserRecord, Depends(get_current_user)]
) -> AsyncGenerator[None]:
    """Route dependency for every handler that changes files the guest can see."""
    _ = await check_agent_access(current_user, agent_id)
    async with guest_write_section(agent_id):
        yield


@asynccontextmanager
async def locked_fresh_agent(agent_id: UUID) -> AsyncGenerator[AgentRecord]:
    """Exclusive lifecycle section yielding the agent row re-read under the lock."""
    await assert_no_active_run(agent_id)
    async with setup_workspace_lock(agent_manager._agent_dir(agent_id)):
        memo_drop("agent", agent_id)
        fresh = await agent_dao.get(agent_id)
        if fresh is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")
        await assert_no_active_run(agent_id)
        await assert_transfer_ready(agent_id)
        yield fresh
