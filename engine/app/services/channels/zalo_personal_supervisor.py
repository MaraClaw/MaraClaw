from pathlib import Path
from typing import Final
from uuid import UUID

import anyio

from app.core.logging import logger
from app.dao.zalo_personal_dao import candidates, claim
from app.runtime.tasks import loop_progress
from app.services.channels.zalo_personal_child import run_child

_COMMAND = ("node", str(Path(__file__).resolve().parents[3] / "connectors/zalo/dist/src/bridge.js"))
PROGRESS_BUDGET_SECONDS: Final = 300.0


async def personal_supervisor() -> None:
    running: set[UUID] = set()
    progress = loop_progress()
    backoff = 1.0
    async with anyio.create_task_group() as tasks:

        async def owned(config_id: UUID) -> None:
            try:
                connection = await claim(config_id)
                if connection is not None:
                    await run_child(connection, _COMMAND)
            except Exception as error:
                logger.warning(
                    "[ZaloPersonal] child_failed config_id={} error_kind={}", config_id, type(error).__name__
                )
                await anyio.sleep(30)
            finally:
                running.discard(config_id)

        while True:
            try:
                found = await candidates()
            except Exception as error:
                logger.warning("[ZaloPersonal] candidates_failed error_kind={}", type(error).__name__)
                await anyio.sleep(backoff)
                backoff = min(backoff * 2, 30)
                continue
            backoff = 1.0
            for connection in found:
                if connection.config_id not in running:
                    running.add(connection.config_id)
                    tasks.start_soon(owned, connection.config_id)
            progress.beat()
            await anyio.sleep(1)
