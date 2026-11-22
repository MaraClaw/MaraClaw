"""Canonical-session delivery authorization: one tracked decision per identity, waited on within a bound per event."""

import asyncio
import contextvars
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import Enum
from functools import partial
from uuid import UUID

from fastapi import HTTPException, WebSocket
from psycopg_pool import PoolTimeout
from redis.exceptions import RedisError

from app.core import access_cache
from app.core.incident_reporting import report_exception
from app.core.logging import logger
from app.db.errors import DbOverloadError
from app.db.session import connection_ctx
from app.runtime.shutdown_budget import join
from app.services import chat_tickets
from app.services.realtime_runtime.fanout import LOCAL_FANOUT_CONCURRENCY

DELIVERY_AUTHORIZATION_TTL_SECONDS = 5.0
# Failures of what a decision reads through, not of the viewer's access: they skip one event and keep the socket.
TRANSIENT_AUTHORIZATION_ERRORS = (DbOverloadError, PoolTimeout, RedisError, OSError)

_clock: Callable[[], float] = time.monotonic


class DeliveryVerdict(Enum):
    """What the delivery check decided for one socket and one event."""

    ALLOWED = "allowed"
    # No decision within this event's wait, or a transient failure: only this event misses the socket, which stays open.
    SKIPPED = "skipped"
    # 401 or an expired binding: the viewer's authentication is gone.
    UNAUTHENTICATED = "unauthenticated"
    # 403 or 404: the viewer lost access to the agent or to the session.
    FORBIDDEN = "forbidden"

    @property
    def close_code(self) -> int | None:
        """The close code of a revocation (4001 or 4003); ``None`` for a verdict that keeps the socket."""
        match self:
            case DeliveryVerdict.UNAUTHENTICATED:
                return 4001
            case DeliveryVerdict.FORBIDDEN:
                return 4003
            case _:
                return None


# Only these statuses revoke a viewer. Any other one (the retryable 423 transfer gate, a 503) is transient.
_REVOCATIONS = {401: DeliveryVerdict.UNAUTHENTICATED, 403: DeliveryVerdict.FORBIDDEN, 404: DeliveryVerdict.FORBIDDEN}

type _DecisionKey = tuple[UUID, UUID, UUID, UUID, int, int, str]


@dataclass(frozen=True, slots=True)
class _CachedAuthorization:
    binding: chat_tickets.ChatTicketBinding
    acl_version: str
    expires_at: float


@dataclass(frozen=True, slots=True)
class _Decision:
    task: asyncio.Task[DeliveryVerdict]
    started_at: float


class _Decisions:
    """The cold decisions in flight on one event loop, at most one per identity and ACL version.

    Each runs as its own task in an empty ``contextvars.Context`` (so it never joins a connection its first waiter
    happens to hold) on one ``connection_ctx`` checkout, at most ``LOCAL_FANOUT_CONCURRENCY`` at once. Waiters only
    watch it: giving up never cancels it, and only ``cancel_pending_decisions`` does.
    """

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop: asyncio.AbstractEventLoop = loop
        self.pending: dict[_DecisionKey, _Decision] = {}
        self.slots: asyncio.Semaphore = asyncio.Semaphore(LOCAL_FANOUT_CONCURRENCY)

    def join(self, binding: chat_tickets.ChatTicketBinding, acl_version: str) -> _Decision:
        key: _DecisionKey = (
            binding.user_id,
            binding.tenant_id,
            binding.agent_id,
            binding.session_id,
            binding.auth_generation,
            binding.membership_generation,
            acl_version,
        )
        decision = self.pending.get(key)
        if decision is None:
            task = asyncio.create_task(
                self._decide(binding), name="shared-chat-authorization", context=contextvars.Context()
            )
            decision = _Decision(task, _clock())
            self.pending[key] = decision
            task.add_done_callback(partial(self._settled, key, decision))
        return decision

    def _settled(self, key: _DecisionKey, decision: _Decision, _task: asyncio.Task[DeliveryVerdict]) -> None:
        if self.pending.get(key) is decision:
            del self.pending[key]

    async def _decide(self, binding: chat_tickets.ChatTicketBinding) -> DeliveryVerdict:
        """Re-authorize one identity on one pool checkout and map the outcome to a verdict; never raise it."""
        async with self.slots:
            try:
                async with connection_ctx():
                    user, agent, _ = await chat_tickets.authorize_identity(binding)
                    await chat_tickets.authorized_session(binding, user, agent)
            except HTTPException as exc:
                verdict = _REVOCATIONS.get(exc.status_code, DeliveryVerdict.SKIPPED)
                if verdict is DeliveryVerdict.SKIPPED:
                    logger.warning("[Realtime] Chat delivery authorization unavailable: status {}", exc.status_code)
                return verdict
            except TRANSIENT_AUTHORIZATION_ERRORS as exc:
                logger.warning("[Realtime] Chat delivery authorization failed transiently: {}", type(exc).__name__)
                return DeliveryVerdict.SKIPPED
            except Exception as exc:
                report_exception(exc, source="realtime", operation="shared-chat-authorization")
                logger.warning("[Realtime] Chat delivery authorization failed: {}", type(exc).__name__)
                return DeliveryVerdict.SKIPPED
        return DeliveryVerdict.ALLOWED


_registry: _Decisions | None = None


def _decisions() -> _Decisions:
    """The running loop's decisions; a new loop starts with none, since tasks never cross loops."""
    global _registry
    loop = asyncio.get_running_loop()
    if _registry is None or _registry.loop is not loop:
        _registry = _Decisions(loop)
    return _registry


async def _wait(decision: asyncio.Task[DeliveryVerdict], seconds: float) -> None:
    """Wait at most ``seconds`` for ``decision``; ``asyncio.wait`` never cancels what it watches."""
    _ = await asyncio.wait({decision}, timeout=seconds)


async def cancel_pending_decisions() -> None:
    """Cancel and await every decision in flight, so none still holds a pool checkout when the pool closes."""
    registry = _decisions()
    while registry.pending:
        decisions = list(registry.pending.values())
        registry.pending.clear()
        for decision in decisions:
            _ = decision.task.cancel()
        await join([decision.task for decision in decisions], "Chat delivery authorization")


def _authorizing_binding(
    websocket: WebSocket, agent_id: str, session_id: str | None
) -> chat_tickets.ChatTicketBinding | None:
    """The socket's binding if its ticket, issued for this agent's canonical ``session_id``, authorizes deliveries."""
    binding = getattr(websocket.state, "chat_binding", None)
    if (
        isinstance(binding, chat_tickets.ChatTicketBinding)
        and binding.authorize_deliveries
        and str(binding.agent_id) == agent_id
        and str(binding.session_id) == session_id
    ):
        return binding
    return None


async def read_delivery_acl_version(agent_id: str, targets: Iterable[tuple[WebSocket, str | None]]) -> str | None:
    """Read the agent ACL version once for a delivery, and only if some target socket authorizes its deliveries.

    Only a socket bound to a canonical session authorizes against the ACL, so a delivery reaching none gains no Redis
    read. Pass the result to ``authorize_chat_delivery`` for every socket of the delivery, before any of them
    authorizes.
    """
    if all(_authorizing_binding(websocket, agent_id, session_id) is None for websocket, session_id in targets):
        return None
    return await access_cache.read_acl_version(UUID(agent_id))


async def authorize_chat_delivery(
    websocket: WebSocket,
    agent_id: str,
    session_id: str | None,
    acl_version: str | None = None,
    *,
    wait_seconds: float,
) -> DeliveryVerdict:
    """Decide whether a canonical-session event may be sent to this socket, waiting at most ``wait_seconds`` counted
    from the start of the decision it joins.

    A socket is allowed without any work unless its ``ChatTicketBinding`` is for this exact agent and session and
    carries ``authorize_deliveries``, which ``chat_tickets.issue`` sets for a canonical session. Such a socket needs a
    binding still inside its own deadline (``UNAUTHENTICATED`` otherwise). A positive decision remembered on the socket for
    ``DELIVERY_AUTHORIZATION_TTL_SECONDS``, and only while the agent ACL version (``access_cache.read_acl_version``) is
    unchanged, then allows it: a revocation that bumps the version takes effect on the next event, and one that does not
    (a session token generation change, an agent expiry) can keep delivering for up to that TTL.

    Without one, the socket joins the cold decision for its identity and ACL version, started by whichever socket
    needed it first. A wait that runs out returns ``SKIPPED`` and leaves the decision running; if it allows, it still
    fills this socket's cache, while events before then skip at once because their wait counts from the same start.
    401 means ``UNAUTHENTICATED`` and 403 or 404 ``FORBIDDEN``; every other failure is ``SKIPPED``. Only positive
    decisions are remembered.

    ``acl_version`` is the value ``read_delivery_acl_version`` read once for the whole delivery; without it the
    version is read here.
    """
    binding = _authorizing_binding(websocket, agent_id, session_id)
    if binding is None:
        return DeliveryVerdict.ALLOWED
    if chat_tickets.is_expired(binding):
        return DeliveryVerdict.UNAUTHENTICATED
    # Read before deciding: a bump landing during the decision must invalidate what it caches.
    if acl_version is None:
        acl_version = await access_cache.read_acl_version(binding.agent_id)
    cached = getattr(websocket.state, "chat_delivery_authorization", None)
    if (
        isinstance(cached, _CachedAuthorization)
        and cached.binding is binding
        and cached.acl_version == acl_version
        and _clock() < cached.expires_at
    ):
        return DeliveryVerdict.ALLOWED
    websocket.state.chat_delivery_authorization = None
    decision = _decisions().join(binding, acl_version)
    remaining = decision.started_at + wait_seconds - _clock()
    if remaining > 0 and not decision.task.done():
        await _wait(decision.task, remaining)
    if not decision.task.done():
        _remember_when_allowed(websocket, binding, acl_version, decision.task)
        return DeliveryVerdict.SKIPPED
    if decision.task.cancelled():
        return DeliveryVerdict.SKIPPED
    verdict = decision.task.result()
    if verdict is DeliveryVerdict.ALLOWED:
        _remember(websocket, binding, acl_version)
    return verdict


def _remember(websocket: WebSocket, binding: chat_tickets.ChatTicketBinding, acl_version: str) -> None:
    websocket.state.chat_delivery_authorization = _CachedAuthorization(
        binding=binding, acl_version=acl_version, expires_at=_clock() + DELIVERY_AUTHORIZATION_TTL_SECONDS
    )


def _remember_when_allowed(
    websocket: WebSocket,
    binding: chat_tickets.ChatTicketBinding,
    acl_version: str,
    decision: asyncio.Task[DeliveryVerdict],
) -> None:
    """Let a decision this socket stopped waiting for still fill its cache if it allows; registered once per decision."""
    if getattr(websocket.state, "chat_delivery_pending", None) is decision:
        return
    websocket.state.chat_delivery_pending = decision

    def remember(task: asyncio.Task[DeliveryVerdict]) -> None:
        if getattr(websocket.state, "chat_delivery_pending", None) is task:
            websocket.state.chat_delivery_pending = None
        if task.cancelled() or task.result() is not DeliveryVerdict.ALLOWED:
            return
        # A rebound socket, or one a fresher decision already answered, keeps what it has.
        if (
            getattr(websocket.state, "chat_binding", None) is binding
            and getattr(websocket.state, "chat_delivery_authorization", None) is None
        ):
            _remember(websocket, binding, acl_version)

    decision.add_done_callback(remember)
