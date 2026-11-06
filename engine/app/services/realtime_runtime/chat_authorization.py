import time
from collections.abc import Callable
from dataclasses import dataclass
from uuid import UUID

from fastapi import HTTPException, WebSocket

from app.core import access_cache
from app.services import chat_tickets
from app.services.shared_chat import shared_session_id

DELIVERY_AUTHORIZATION_TTL_SECONDS = 5.0

_clock: Callable[[], float] = time.monotonic


@dataclass(frozen=True, slots=True)
class _CachedAuthorization:
    binding: chat_tickets.ChatTicketBinding
    acl_version: str
    expires_at: float


def is_shared_target(agent_id: str, session_id: str | None) -> bool:
    try:
        return UUID(session_id or "") == shared_session_id(UUID(agent_id))
    except ValueError:
        return False


async def authorize_chat_delivery(websocket: WebSocket, agent_id: str, session_id: str | None) -> bool:
    """Decide whether a Shared-session event may be sent to this socket.

    A positive decision is remembered on the socket for ``DELIVERY_AUTHORIZATION_TTL_SECONDS`` and
    only while the agent ACL version (``access_cache.read_acl_version``) is unchanged, so any
    revocation that bumps the version (agent policy columns, agent permission grants, tenant, user
    role/activation/membership and department changes) takes effect on the next event. Revocations
    that do not bump it (for example a session token generation change or an agent expiry) can
    keep delivering for up to that TTL. Negative results are never cached.
    """
    if not is_shared_target(agent_id, session_id):
        return True
    binding = getattr(websocket.state, "chat_binding", None)
    if not isinstance(binding, chat_tickets.ChatTicketBinding):
        return False
    if str(binding.agent_id) != agent_id or str(binding.session_id) != session_id:
        return False
    # Read before authorizing: a bump landing during authorization must invalidate what we cache.
    acl_version = await access_cache.read_acl_version(binding.agent_id)
    cached = getattr(websocket.state, "chat_delivery_authorization", None)
    if (
        isinstance(cached, _CachedAuthorization)
        and cached.binding is binding
        and cached.acl_version == acl_version
        and _clock() < cached.expires_at
    ):
        return True
    websocket.state.chat_delivery_authorization = None
    try:
        user, agent, _ = await chat_tickets.authorize(binding)
        await chat_tickets.authorized_session(binding, user, agent)
    except HTTPException:
        return False
    websocket.state.chat_delivery_authorization = _CachedAuthorization(
        binding=binding, acl_version=acl_version, expires_at=_clock() + DELIVERY_AUTHORIZATION_TTL_SECONDS
    )
    return True
