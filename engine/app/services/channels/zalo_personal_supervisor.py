from collections.abc import Callable
from pathlib import Path
from random import random as system_random
from time import monotonic
from typing import Final
from uuid import UUID

import anyio

from app.core.logging import logger
from app.dao.zalo_personal_dao import candidates, claim
from app.runtime.tasks import ReconnectBackoff, loop_progress
from app.services.channels.zalo_personal_child import run_child

_COMMAND = ("node", str(Path(__file__).resolve().parents[3] / "connectors/zalo/dist/src/bridge.js"))
_COOLDOWN_BASE_SECONDS: Final = 30.0
_COOLDOWN_CAP_SECONDS: Final = 300.0
_HEALTHY_CHILD_SECONDS: Final = 120.0
PROGRESS_BUDGET_SECONDS: Final = 300.0


async def personal_supervisor(
    clock: Callable[[], float] = monotonic, random: Callable[[], float] = system_random
) -> None:
    running: set[UUID] = set()
    cooldowns: dict[UUID, float] = {}
    escalation: dict[UUID, ReconnectBackoff] = {}
    progress = loop_progress()
    backoff = 1.0
    async with anyio.create_task_group() as tasks:

        async def owned(config_id: UUID) -> None:
            started = clock()
            try:
                connection = await claim(config_id)
                if connection is not None:
                    await run_child(connection, _COMMAND)
            except Exception as error:
                policy = escalation.get(config_id)
                if policy is None:
                    policy = escalation[config_id] = ReconnectBackoff(
                        base_seconds=_COOLDOWN_BASE_SECONDS,
                        cap_seconds=_COOLDOWN_CAP_SECONDS,
                        healthy_seconds=_HEALTHY_CHILD_SECONDS,
                        random=random,
                        clock=clock,
                    )
                cooldowns[config_id] = clock() + policy.next_delay(started)
                logger.warning(
                    "[ZaloPersonal] child_failed config_id={} error_kind={}", config_id, type(error).__name__
                )
            else:
                if (policy := escalation.get(config_id)) is not None:
                    policy.settle(started)
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
            now = clock()
            current = {connection.config_id for connection in found} | running
            for config_id in cooldowns.keys() - current:
                del cooldowns[config_id]
            for config_id in escalation.keys() - current:
                del escalation[config_id]
            for config_id in [config_id for config_id, until in cooldowns.items() if until <= now]:
                del cooldowns[config_id]
            for connection in found:
                if connection.config_id not in running and connection.config_id not in cooldowns:
                    running.add(connection.config_id)
                    tasks.start_soon(owned, connection.config_id)
            progress.beat()
            await anyio.sleep(1)
