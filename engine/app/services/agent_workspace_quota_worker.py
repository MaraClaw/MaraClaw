"""Periodic measurement of every tier-governed agent's workspace disk usage (``agent_workspace_quota``)."""

from typing import Final
from uuid import UUID

from anyio import sleep
from psycopg import Error as PsycopgError

from app.config import get_settings
from app.core.logging import logger, new_trace_id
from app.dao.agent_dao import agent_dao
from app.db.errors import DbError
from app.runtime.tasks import capped_backoff, loop_progress
from app.services.agent_workspace_quota import LayerReads, measure_agent

PAGE_SIZE: Final = 50
# A pass measures agents one at a time, so the readiness budget is the sleep between passes plus this allowance.
PASS_ALLOWANCE_SECONDS: Final = 3600.0
DB_RETRY_BASE_SECONDS: Final = 1.0
DB_RETRY_CAP_SECONDS: Final = 30.0


def progress_budget_seconds() -> float:
    return get_settings().AGENT_WORKSPACE_QUOTA_INTERVAL_SECONDS + PASS_ALLOWANCE_SECONDS


async def start_agent_workspace_quota_worker() -> None:
    """Measure every agent in id order, then sleep the interval; beat only after a completed pass.

    One agent's failure is logged and the pass continues. A database error reading a page is retried in place with
    capped backoff, resumes from the same cursor and never beats. Each pass shares one ``LayerReads``, so a stalled
    Docker daemon costs the pass one layer-read timeout instead of one per agent.
    """
    progress = loop_progress()
    failures = 0
    cursor: UUID | None = None
    layers = LayerReads()
    new_trace_id()
    while True:
        try:
            page = await agent_dao.list_workspace_quota_page(cursor, PAGE_SIZE)
        except (DbError, PsycopgError, OSError) as exc:
            logger.warning("[WorkspaceQuota] listing agents failed; will retry: {}", type(exc).__name__)
            await sleep(capped_backoff(failures, base_seconds=DB_RETRY_BASE_SECONDS, cap_seconds=DB_RETRY_CAP_SECONDS))
            failures += 1
            continue
        failures = 0
        for agent in page:
            try:
                await measure_agent(agent, layers)
            except Exception as exc:
                logger.bind(exception_type=type(exc).__name__).warning(
                    "[WorkspaceQuota] measuring failed agent={}", agent.id
                )
        if len(page) == PAGE_SIZE:
            cursor = page[-1].id
            continue
        cursor = None
        progress.beat()
        await sleep(get_settings().AGENT_WORKSPACE_QUOTA_INTERVAL_SECONDS)
        layers = LayerReads()
        new_trace_id()
