"""In-process health of LLM provider connections: stop sending turns to a model whose provider is failing.

A provider connection is one configured model row (``LLMModelRecord.id``): its provider, endpoint, credential and
model. Keying by row keeps one tenant's exhausted key from shutting out another tenant, and a model that is
overloaded from taking its healthy fallback on the same vendor down with it.

Each connection has a circuit, like ``redis_cache.CacheBreaker``. ``failure_threshold`` consecutive retryable failures
(``failover.classify_error`` decides, status code first) open it. While it is open every call is refused at once, and
``router.select_turn_model`` routes new turns to the agent's fallback model. After ``cooldown`` seconds exactly one
caller is admitted as the probe: its success closes the circuit and its failure reopens it for another cooldown, while
everyone else is still refused. State lives in this process only and never touches Redis.

Nothing here waits or retries: waiting stays in ``retry_policy.py``, and a refused call returns immediately.
"""

from __future__ import annotations

import math
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from functools import cache
from typing import final
from uuid import UUID

from app.config import get_settings
from app.core.logging import logger
from app.records.llm import LLMModelRecord

from .base import LLMError
from .failover import FailoverErrorType, classify_error
from .retry_policy import LLMHTTPError


class Admission(StrEnum):
    CALL = "call"
    PROBE = "probe"


class ProviderUnavailableError(LLMError):
    """A call was refused because its provider's circuit is open; no request was sent.

    The message says the provider is unavailable so ``failover.classify_error`` reads the ``[LLM Error] ...`` reply as
    retryable and ``call_llm_with_failover`` can still use the fallback model.
    """

    def __init__(self, retry_in: float) -> None:
        wait = f"try again in {math.ceil(retry_in)} s" if retry_in > 0 else "try again shortly"
        super().__init__(f"LLM provider is unavailable after repeated failures; {wait}")


@dataclass(slots=True)
class _Circuit:
    failures: int = 0
    opened: bool = False
    probing: bool = False
    retry_at: float = 0.0


@final
class ProviderHealth:
    """Closed, open or half-open circuit per provider connection; the clock is injectable for tests.

    A connection with no recorded failures has no entry. Only the probe closes or reopens an open circuit: a call
    admitted before it opened says nothing new, and a probe that proved nothing (cancelled, or failed for a reason
    unrelated to the provider) hands the slot to the next caller.
    """

    def __init__(self, *, failure_threshold: int, cooldown: float, clock: Callable[[], float] = time.monotonic) -> None:
        self._failure_threshold: int = failure_threshold
        self._cooldown: float = cooldown
        self._clock: Callable[[], float] = clock
        self._circuits: dict[UUID, _Circuit] = {}

    def admit(self, key: UUID) -> Admission | None:
        """Admit the caller, or return None to refuse it."""
        circuit = self._circuits.get(key)
        if circuit is None or not circuit.opened:
            return Admission.CALL
        if circuit.probing or self._clock() < circuit.retry_at:
            return None
        circuit.probing = True
        return Admission.PROBE

    def refusing(self, key: UUID) -> bool:
        """Whether a call made right now would be refused."""
        circuit = self._circuits.get(key)
        return circuit is not None and circuit.opened and (circuit.probing or self._clock() < circuit.retry_at)

    def retry_in(self, key: UUID) -> float:
        """Seconds until a probe may be admitted; 0 when none is waiting out a cooldown."""
        circuit = self._circuits.get(key)
        if circuit is None or not circuit.opened:
            return 0.0
        return max(0.0, circuit.retry_at - self._clock())

    def succeeded(self, key: UUID, admission: Admission) -> bool:
        """Record an answer from the provider. True when it closed an open circuit."""
        circuit = self._circuits.get(key)
        if circuit is None:
            return False
        if admission is Admission.PROBE:
            del self._circuits[key]
            return True
        if not circuit.opened:
            del self._circuits[key]
        return False

    def failed(self, key: UUID, admission: Admission) -> bool:
        """Record a retryable failure. True when it opened or reopened the circuit."""
        circuit = self._circuits.setdefault(key, _Circuit())
        if admission is Admission.PROBE:
            self._open(circuit)
            return True
        if circuit.opened:
            return False
        circuit.failures += 1
        if circuit.failures < self._failure_threshold:
            return False
        self._open(circuit)
        return True

    def abandoned(self, key: UUID, admission: Admission) -> None:
        """Release the probe slot when the probe proved nothing about the provider."""
        circuit = self._circuits.get(key)
        if circuit is not None and admission is Admission.PROBE:
            circuit.probing = False

    def _open(self, circuit: _Circuit) -> None:
        circuit.opened = True
        circuit.probing = False
        circuit.retry_at = self._clock() + self._cooldown


@cache
def health_tracker() -> ProviderHealth:
    """The process-wide tracker, sized from settings on first use. Tests clear it with ``reset_provider_health``."""
    settings = get_settings()
    return ProviderHealth(
        failure_threshold=settings.LLM_PROVIDER_FAILURE_THRESHOLD, cooldown=settings.LLM_PROVIDER_COOLDOWN_SECONDS
    )


def reset_provider_health() -> None:
    """Test helper: forget every circuit so one test's outage cannot leave a provider open for the next."""
    health_tracker.cache_clear()


def model_key(model: LLMModelRecord | None) -> UUID | None:
    """The circuit key of a model row; a model without a row id is not tracked."""
    if model is None:
        return None
    model_id = getattr(model, "id", None)
    return model_id if isinstance(model_id, UUID) else None


def is_unavailable(model: LLMModelRecord) -> bool:
    """Whether a call to this model would be refused right now (its circuit is open and no probe is due)."""
    key = model_key(model)
    return key is not None and health_tracker().refusing(key)


def ensure_available(model: LLMModelRecord) -> None:
    """Raise ``ProviderUnavailableError`` while the circuit refuses calls; check before charging for a request."""
    key = model_key(model)
    tracker = health_tracker()
    if key is not None and tracker.refusing(key):
        raise ProviderUnavailableError(tracker.retry_in(key))


async def guarded[T](model: LLMModelRecord, call: Callable[[], Awaitable[T]]) -> T:
    """Run one provider request under the model's circuit and report how it ended.

    Raises ``ProviderUnavailableError`` instead of calling while the circuit refuses. ``call`` does only the provider
    I/O. A retryable failure counts toward opening the circuit, an answer (success, or a 4xx other than 429) counts
    as the provider being up, and anything else, cancellation included, only frees a probe slot.
    """
    key = model_key(model)
    if key is None:
        return await call()
    tracker = health_tracker()
    admission = tracker.admit(key)
    if admission is None:
        raise ProviderUnavailableError(tracker.retry_in(key))
    try:
        result = await call()
    except Exception as error:
        _record_failure(model, key, admission, error)
        raise
    except BaseException:
        tracker.abandoned(key, admission)
        raise
    _record_answer(model, key, admission)
    return result


def _record_failure(model: LLMModelRecord, key: UUID, admission: Admission, error: Exception) -> None:
    tracker = health_tracker()
    kind = classify_error(error)
    if kind is FailoverErrorType.RETRYABLE:
        if tracker.failed(key, admission):
            logger.warning(
                "[LLM health] {}/{} circuit open for {:.0f}s after {}: {}",
                model.provider,
                model.model,
                tracker.retry_in(key),
                "a failed probe" if admission is Admission.PROBE else "consecutive failures",
                type(error).__name__,
            )
    elif isinstance(error, LLMHTTPError) and kind is FailoverErrorType.NON_RETRYABLE:
        _record_answer(model, key, admission)
    else:
        tracker.abandoned(key, admission)


def _record_answer(model: LLMModelRecord, key: UUID, admission: Admission) -> None:
    if health_tracker().succeeded(key, admission):
        logger.info("[LLM health] {}/{} answered its probe; circuit closed", model.provider, model.model)


__all__ = [
    "Admission",
    "ProviderHealth",
    "ProviderUnavailableError",
    "ensure_available",
    "guarded",
    "health_tracker",
    "is_unavailable",
    "model_key",
    "reset_provider_health",
]
