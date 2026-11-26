from collections.abc import Callable
from pathlib import Path
from random import random as system_random
from time import monotonic
from typing import Final
from uuid import UUID

import anyio

from app.config import get_settings
from app.core.logging import logger
from app.dao.zalo_personal_dao import candidates, claim
from app.records.zalo_personal import ZaloConnection
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
    settings = get_settings()
    ceiling = settings.ZALO_PERSONAL_MAX_RUNNING_CHILDREN
    per_poll = settings.ZALO_PERSONAL_MAX_STARTS_PER_POLL
    held = False
    progress = loop_progress()
    backoff = 1.0
    async with anyio.create_task_group() as tasks:

        def failed(config_id: UUID, started: float, error: Exception) -> None:
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
            logger.warning("[ZaloPersonal] child_failed config_id={} error_kind={}", config_id, type(error).__name__)

        async def owned(connection: ZaloConnection, started: float) -> None:
            try:
                await run_child(connection, _COMMAND)
            except Exception as error:
                failed(connection.config_id, started, error)
            else:
                if (policy := escalation.get(connection.config_id)) is not None:
                    policy.settle(started)
            finally:
                running.discard(connection.config_id)

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
            waiting = [
                connection.config_id
                for connection in found
                if connection.config_id not in running and connection.config_id not in cooldowns
            ]
            # Waiting connections are listed longest waiting first. Only a claim that wins a child counts against the
            # pace, so a connection whose claim is declined never holds back the ones behind it.
            allowed = min(ceiling - len(running), per_poll)
            started_now = tried = 0
            for config_id in waiting:
                if started_now == allowed:
                    break
                tried += 1
                started = clock()
                try:
                    connection = await claim(config_id)
                except Exception as error:
                    failed(config_id, started, error)
                    continue
                if connection is None:
                    continue
                started_now += 1
                running.add(config_id)
                tasks.start_soon(owned, connection, started)
            deferred = len(waiting) - tried
            if len(running) >= ceiling and deferred and not held:
                logger.warning(
                    "[ZaloPersonal] fleet_ceiling_reached running={} ceiling={} waiting={}",
                    len(running),
                    ceiling,
                    deferred,
                )
            held = len(running) >= ceiling and deferred > 0
            progress.beat()
            await anyio.sleep(1)
