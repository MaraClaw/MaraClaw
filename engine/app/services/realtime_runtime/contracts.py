"""Fixed Redis routing shapes without constraining websocket event payloads."""

from collections.abc import Awaitable
from typing import NotRequired, Protocol

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
