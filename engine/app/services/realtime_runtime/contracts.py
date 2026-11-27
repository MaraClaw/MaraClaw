"""Fixed Redis routing shapes without constraining websocket event payloads."""

import json
from collections.abc import Awaitable, Mapping
from dataclasses import dataclass
from typing import NotRequired, Protocol

from fastapi import WebSocket
from pydantic import BaseModel, TypeAdapter
from typing_extensions import TypedDict


class RealtimeEnvelope(TypedDict):
    agent_id: str
    message: dict[str, object]
    session_id: NotRequired[str | None]
    user_id: NotRequired[str | None]
    origin_instance_id: NotRequired[str | None]


class RealtimePresence(TypedDict, total=False, extra_items=str):
    agent_id: str
    session_id: str
    user_id: str
    instance_id: str


presence_adapter = TypeAdapter(RealtimePresence)

# One entry of a session index: (connection_id, instance_id, user_id), which is all a chunk's routing needs to know
# about a socket watching the session. The member is immutable for the life of the connection, so removing it needs
# the same three values; ``RealtimeRouter.registered`` hands them out.
type SessionMember = tuple[str, str, str]
session_member_adapter = TypeAdapter(SessionMember)


def encode_session_member(connection_id: str, presence: Mapping[str, str]) -> str:
    return json.dumps(
        [connection_id, presence.get("instance_id", ""), presence.get("user_id", "")], separators=(",", ":")
    )


class ReceivedEnvelope(BaseModel):
    """Require existing delivery keys but preserve legacy JSON value passthrough."""

    agent_id: object
    message: object
    session_id: object = None
    user_id: object = None


class LocalDeliverer(Protocol):
    def __call__(
        self,
        *,
        agent_id: str,
        payload: dict[str, object],
        session_id: str | None,
        user_id: str | None,
    ) -> Awaitable[object]: ...


@dataclass(frozen=True, slots=True)
class RoutingOutcome:
    """What routing one event did.

    ``local_targets`` matched the session/user filter; sockets authorization refused or revoked count nowhere else.
    ``failed`` sends raised or timed out (including the timeout that evicted a socket); ``evicted`` sockets were
    evicted by this delivery or found already evicted in its snapshot. ``skipped`` sockets got no canonical-session
    decision within their wait, or a decision that failed transiently, so only this event passed them by.
    ``remote_publish_errors`` counts instance channels whose ``redis.publish`` raised, and ``publication_failed`` a
    publication that raised before routing finished.
    """

    local_targets: int = 0
    delivered: int = 0
    failed: int = 0
    evicted: int = 0
    skipped: int = 0
    remote_instances: int = 0
    remote_publish_errors: int = 0
    publication_failed: bool = False

    @property
    def dropped(self) -> bool:
        """Some recipient the event was meant for never got it. An event nobody could receive is not dropped."""
        return bool(
            self.failed or self.evicted or self.skipped or self.remote_publish_errors or self.publication_failed
        )


class SendObserver(Protocol):
    """The owner of local socket state, told about each bounded send. Every method is synchronous: no await between
    the liveness check and the send, and an eviction completes before the delivery moves on."""

    def is_live(self, websocket: WebSocket) -> bool: ...

    def sent(self, websocket: WebSocket) -> None: ...

    def timed_out(self, agent_id: str, websocket: WebSocket) -> bool:
        """Record a send that hit its bound; True when this timeout evicted the socket."""
        ...

    def refused(self, agent_id: str, websocket: WebSocket, close_code: int) -> None:
        """Withdraw a socket whose canonical-session access was revoked and close it with ``close_code`` (4001 or 4003)
        from a task the observer owns; never awaited by the delivery that found the revocation."""
        ...
