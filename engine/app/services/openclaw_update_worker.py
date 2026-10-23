"""Worker-only loop that drives pending and interrupted OpenClaw upgrade runs."""

from __future__ import annotations

import anyio

from app.core.logging import logger, new_trace_id
from app.core.row_memo import clear_row_memo
from app.services.agent_manager import agent_manager
from app.services.openclaw_update_docker import UpgradeDocker
from app.services.openclaw_update_executor import UpgradeExecutor
from app.services.openclaw_update_store import PostgresUpgradeStore, UpgradeStore

POLL_SECONDS = 5.0


async def drive_active_runs(store: UpgradeStore, executor: UpgradeExecutor) -> int:
    """Process every active run once; one failing run never starves the others."""
    runs = await store.list_active()
    for run in runs:
        new_trace_id()
        clear_row_memo()
        try:
            await executor.process(run.id)
        except Exception:
            logger.exception("[OpenClawUpdate] run crashed run={} agent={}", run.id, run.agent_id)
    return len(runs)


async def start_openclaw_update_worker() -> None:
    client = agent_manager.docker_client
    store = PostgresUpgradeStore()
    if client is None:
        logger.warning("[OpenClawUpdate] Docker unavailable; upgrade runs will not progress")
    executor = UpgradeExecutor(store, UpgradeDocker(client), agent_manager) if client is not None else None
    while True:
        if executor is not None:
            try:
                _ = await drive_active_runs(store, executor)
            except Exception:
                logger.exception("[OpenClawUpdate] worker pass failed")
        await anyio.sleep(POLL_SECONDS)
