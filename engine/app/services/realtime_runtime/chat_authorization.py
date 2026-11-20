"""Shared-session delivery authorization: one tracked decision per identity, waited on within a bound per event."""

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
from app.services.shared_chat import shared_session_id

DELIVERY_AUTHORIZATION_TTL_SECONDS = 5.0
# Failures of what a decision reads through, not of the viewer's access: they skip one event and keep the socket.
TRANSIENT_AUTHORIZATION_ERRORS = (DbOverloadError, PoolTimeout, RedisError, OSError)

_clock: Callable[[], float] = time.monotonic


class DeliveryVerdict(Enum):
    """What the shared-session check decided for one socket and one event."""

    ALLOWED = "allowed"
    # The socket holds no binding for this agent's shared session: not a viewer, passed over without a trace.
    NOT_VIEWER = "not_viewer"
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
                    logger.warning("[Realtime] Shared chat authorization unavailable: status {}", exc.status_code)
                return verdict
            except TRANSIENT_AUTHORIZATION_ERRORS as exc:
                logger.warning("[Realtime] Shared chat authorization failed transiently: {}", type(exc).__name__)
                return DeliveryVerdict.SKIPPED
            except Exception as exc:
                report_exception(exc, source="realtime", operation="shared-chat-authorization")
                logger.warning("[Realtime] Shared chat authorization failed: {}", type(exc).__name__)
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
        await join([decision.task for decision in decisions], "Shared chat authorization")


def is_shared_target(agent_id: str, session_id: str | None) -> bool:
    try:
        return UUID(session_id or "") == shared_session_id(UUID(agent_id))
    except ValueError:
        return False


async def read_shared_acl_version(agent_id: str, session_ids: Iterable[str | None]) -> str | None:
    """Read the agent ACL version once for a delivery, and only if some target is the shared session.

    Ordinary sessions never authorize against the ACL, so they gain no Redis read. Pass the result to
    ``authorize_chat_delivery`` for every socket of the delivery, before any of them authorizes.
    """
    if not any(is_shared_target(agent_id, session_id) for session_id in set(session_ids)):
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
    """Decide whether a Shared-session event may be sent to this socket, waiting at most ``wait_seconds`` counted from
    the start of the decision it joins.

    Ordinary sessions are allowed without any work. A shared-session socket needs its ``ChatTicketBinding`` for this
    exact agent and session (``NOT_VIEWER`` otherwise) and a binding still inside its own deadline
    (``UNAUTHENTICATED`` otherwise). A positive decision remembered on the socket for
    ``DELIVERY_AUTHORIZATION_TTL_SECONDS``, and only while the agent ACL version (``access_cache.read_acl_version``) is
    unchanged, then allows it: a revocation that bumps the version takes effect on the next event, and one that does not
    (a session token generation change, an agent expiry) can keep delivering for up to that TTL.

    Without one, the socket joins the cold decision for its identity and ACL version, started by whichever socket
    needed it first. A wait that runs out returns ``SKIPPED`` and leaves the decision running; if it allows, it still
    fills this socket's cache, while events before then skip at once because their wait counts from the same start.
    401 means ``UNAUTHENTICATED`` and 403 or 404 ``FORBIDDEN``; every other failure is ``SKIPPED``. Only positive
    decisions are remembered.

    ``acl_version`` is the value ``read_shared_acl_version`` read once for the whole delivery; without it the version
    is read here.
    """
    if not is_shared_target(agent_id, session_id):
        return DeliveryVerdict.ALLOWED
    binding = getattr(websocket.state, "chat_binding", None)
    if not isinstance(binding, chat_tickets.ChatTicketBinding):
        return DeliveryVerdict.NOT_VIEWER
    if str(binding.agent_id) != agent_id or str(binding.session_id) != session_id:
        return DeliveryVerdict.NOT_VIEWER
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
