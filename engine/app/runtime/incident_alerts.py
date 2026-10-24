"""Runtime-owned, bounded incident delivery, independent of producer transactions."""

import asyncio
from collections import OrderedDict
from contextvars import Context
from time import monotonic

from redis.asyncio import Redis

from app.core.events import get_cache_redis
from app.core.incident_reporting import IncidentSink, install_sink, uninstall_sink
from app.core.logging import logger, new_trace_id
from app.core.redis_cache import cache_key
from app.services.incident_report import IncidentReport

_COOLDOWN_SECONDS = 900
_LOCAL_LIMIT = 256
_REDIS_TIMEOUT = 0.9
_DRAIN_SECONDS = 20.0
_RELEASE = "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) else return 0 end"


class IncidentAlerts:
    def __init__(self) -> None:
        self._queue: asyncio.Queue[IncidentReport] = asyncio.Queue(maxsize=16)
        self._consumer: asyncio.Task[None] | None = None
        self._sink: IncidentSink = self._enqueue
        self._cooldowns: OrderedDict[str, float] = OrderedDict()
        self._outstanding: int = 0
        self._overflow: int = 0

    def start(self) -> None:
        """Start only after the pool exists; never join the producers' OwnedTasks."""
        self._consumer = asyncio.create_task(self._consume(), name="incident-alerts", context=Context())
        install_sink(self._sink)

    def _enqueue(self, report: IncidentReport) -> None:
        consumer = self._consumer
        if consumer is None or consumer.done():
            self._overflow += 1
            return
        try:
            self._queue.put_nowait(report)
        except asyncio.QueueFull:
            self._overflow += 1
        else:
            self._outstanding += 1

    async def _consume(self) -> None:
        while True:
            report = await self._queue.get()
            try:
                new_trace_id()
                await self._process(report)
                self._outstanding -= 1
            except Exception as error:
                self._outstanding -= 1
                logger.warning("[incidents] Event processing failed: {}", type(error).__name__)
            finally:
                self._queue.task_done()

    async def _process(self, report: IncidentReport) -> None:
        now = monotonic()
        while self._cooldowns and next(iter(self._cooldowns.values())) <= now:
            self._cooldowns.popitem(last=False)
        if report.fingerprint in self._cooldowns:
            return
        self._cooldowns[report.fingerprint] = now + _COOLDOWN_SECONDS
        if len(self._cooldowns) > _LOCAL_LIMIT:
            self._cooldowns.popitem(last=False)

        key = cache_key("incident", report.fingerprint)
        token = str(report.incident_id)
        owner: Redis | None = None
        try:
            async with asyncio.timeout(_REDIS_TIMEOUT):
                redis = await get_cache_redis()
                claimed = await redis.set(key, token, nx=True, ex=_COOLDOWN_SECONDS)
            if claimed is None or claimed is False:
                return
            owner = redis
        except Exception as error:
            logger.warning("[incidents] Shared cooldown unavailable: {}", type(error).__name__)

        delivered = False
        try:
            from app.services.incident_delivery import deliver_incident

            delivered = await deliver_incident(report)
        except Exception as error:
            logger.warning("[incidents] Delivery failed: {}", type(error).__name__)
        finally:
            if not delivered:
                self._cooldowns.pop(report.fingerprint, None)
                if owner is not None:
                    try:
                        async with asyncio.timeout(_REDIS_TIMEOUT):
                            await owner.eval(_RELEASE, 1, key, token)
                    except Exception as error:
                        logger.warning("[incidents] Cooldown release failed: {}", type(error).__name__)

    async def stop(self) -> None:
        """Uninstall, drain within the deadline, then cancel/join only our consumer."""
        uninstall_sink(self._sink)
        consumer = self._consumer
        if consumer is None:
            return
        joined = asyncio.create_task(self._queue.join(), name="incident-drain", context=Context())
        try:
            _ = await asyncio.wait((joined, consumer), timeout=_DRAIN_SECONDS, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in (joined, consumer):
                if not task.done():
                    _ = task.cancel()
            results = await asyncio.gather(joined, consumer, return_exceptions=True)
            for result in results:
                if isinstance(result, Exception):
                    logger.warning("[incidents] Consumer stopped: {}", type(result).__name__)
            lost = self._outstanding + self._overflow
            while not self._queue.empty():
                _ = self._queue.get_nowait()
                self._queue.task_done()
            self._outstanding = 0
            self._overflow = 0
            self._consumer = None
            if lost:
                logger.warning("[incidents] Lost {} events during overflow or shutdown", lost)
