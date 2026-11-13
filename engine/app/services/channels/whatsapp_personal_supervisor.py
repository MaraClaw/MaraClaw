from collections.abc import Callable
from pathlib import Path
from time import monotonic
from typing import Final
from uuid import UUID

import anyio

from app.core.logging import logger
from app.dao.whatsapp_personal_dao import candidates, claim
from app.runtime.tasks import loop_progress
from app.services.channels.whatsapp_personal_child import run_child
from app.services.channels.whatsapp_personal_diagnostics import error_summary

_COMMAND = ("node", str(Path(__file__).resolve().parents[3] / "connectors/whatsapp/dist/src/bridge.js"))
_POLL_SECONDS = 1.0
_BACKOFF_START_SECONDS = 1.0
_BACKOFF_MAX_SECONDS = 30.0
_COOLDOWN_SECONDS = 30.0
PROGRESS_BUDGET_SECONDS: Final = 300.0


async def personal_supervisor(clock: Callable[[], float] = monotonic) -> None:
    running: set[UUID] = set()
    cooldowns: dict[UUID, float] = {}
    backoff = _BACKOFF_START_SECONDS
    progress = loop_progress()
    async with anyio.create_task_group() as tasks:

        async def owned(config_id: UUID) -> None:
            try:
                connection = await claim(config_id)
                if connection is not None:
                    await run_child(connection, _COMMAND)
            except Exception as error:
                # Unclassified: leave stored credentials alone (no fail_connection) and keep siblings running.
                cooldowns[config_id] = clock() + _COOLDOWN_SECONDS
                logger.warning(
                    "[WhatsAppPersonal] connection_task_failed config_id={} error_kind={}",
                    config_id,
                    error_summary(error).get("error_kind", "other"),
                )
            finally:
                running.discard(config_id)

        while True:
            try:
                found = await candidates()
            except Exception as error:
                logger.warning(
                    "[WhatsAppPersonal] candidates_failed error_kind={} retry_seconds={}",
                    error_summary(error).get("error_kind", "other"),
                    backoff,
                )
                await anyio.sleep(backoff)
                backoff = min(backoff * 2, _BACKOFF_MAX_SECONDS)
                continue
            backoff = _BACKOFF_START_SECONDS
            now = clock()
            for config_id in [config_id for config_id, until in cooldowns.items() if until <= now]:
                del cooldowns[config_id]
            for connection in found:
                if connection.config_id not in running and connection.config_id not in cooldowns:
                    running.add(connection.config_id)
                    tasks.start_soon(owned, connection.config_id)
            progress.beat()
            await anyio.sleep(_POLL_SECONDS)
