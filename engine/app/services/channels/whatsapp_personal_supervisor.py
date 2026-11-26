from collections.abc import Callable
from pathlib import Path
from random import random as system_random
from time import monotonic
from typing import Final
from uuid import UUID

import anyio

from app.config import get_settings
from app.core.logging import logger
from app.dao.personal_bridge_fleet import FleetFullError
from app.dao.whatsapp_personal_dao import candidates, claim
from app.records.whatsapp_personal import PersonalConnection
from app.runtime.tasks import ReconnectBackoff, loop_progress
from app.services.channels.whatsapp_personal_child import run_child
from app.services.channels.whatsapp_personal_diagnostics import error_summary

_COMMAND = ("node", str(Path(__file__).resolve().parents[3] / "connectors/whatsapp/dist/src/bridge.js"))
_POLL_SECONDS = 1.0
_BACKOFF_START_SECONDS = 1.0
_BACKOFF_MAX_SECONDS = 30.0
_COOLDOWN_BASE_SECONDS: Final = 30.0
_COOLDOWN_CAP_SECONDS: Final = 300.0
_HEALTHY_CHILD_SECONDS: Final = 120.0
_CLAIM_TIMEOUT_SECONDS: Final = 10.0
PROGRESS_BUDGET_SECONDS: Final = 300.0


async def personal_supervisor(
    clock: Callable[[], float] = monotonic, random: Callable[[], float] = system_random
) -> None:
    running: set[UUID] = set()
    cooldowns: dict[UUID, float] = {}
    escalation: dict[UUID, ReconnectBackoff] = {}
    backoff = _BACKOFF_START_SECONDS
    settings = get_settings()
    ceiling = settings.WHATSAPP_PERSONAL_MAX_RUNNING_CHILDREN
    per_poll = settings.WHATSAPP_PERSONAL_MAX_STARTS_PER_POLL
    held = False
    progress = loop_progress()
    async with anyio.create_task_group() as tasks:

        def failed(config_id: UUID, started: float, error: Exception) -> None:
            # Unclassified: leave stored credentials alone (no fail_connection) and keep siblings running.
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
                "[WhatsAppPersonal] connection_task_failed config_id={} error_kind={}",
                config_id,
                error_summary(error).get("error_kind", "other"),
            )

        async def owned(connection: PersonalConnection, started: float) -> None:
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
            # pace, so a connection whose claim is declined never holds back the ones behind it. A full fleet (other
            # processes count too) and a claim that outlasts its budget both end the poll's claims: their cause is shared,
            # so the connections behind would only wait out the budget in turn. The next poll starts with them.
            allowed = min(ceiling - len(running), per_poll)
            started_now = deferred = 0
            fleet_live: int | None = None
            for position, config_id in enumerate(waiting):
                if started_now == allowed:
                    deferred = len(waiting) - position
                    break
                started = clock()
                try:
                    with anyio.fail_after(_CLAIM_TIMEOUT_SECONDS):
                        connection = await claim(config_id)
                except FleetFullError as full:
                    fleet_live, deferred = full.live, len(waiting) - position
                    break
                except TimeoutError as error:
                    failed(config_id, started, error)
                    deferred = len(waiting) - position - 1
                    logger.warning("[WhatsAppPersonal] claim_abandoned config_id={} deferred={}", config_id, deferred)
                    break
                except Exception as error:
                    failed(config_id, started, error)
                    continue
                if connection is None:
                    continue
                started_now += 1
                running.add(config_id)
                tasks.start_soon(owned, connection, started)
            capped = fleet_live is not None or (len(running) >= ceiling and deferred > 0)
            if capped and not held:
                logger.warning(
                    "[WhatsAppPersonal] fleet_ceiling_reached running={} ceiling={} waiting={}",
                    len(running) if fleet_live is None else fleet_live,
                    ceiling,
                    deferred,
                )
            held = capped
            progress.beat()
            await anyio.sleep(_POLL_SECONDS)
