"""Typed views of the untyped IM SDKs (``dingtalk_stream``) and of their wire payloads.

The SDKs ship no annotations, so each one is adapted here at a single boundary: callers
receive these Protocols instead of SDK objects whose attributes the checker infers as
``None`` or ``Unknown``. The narrowing helpers type ``isinstance`` checks on decoded payloads
without claiming anything about the items.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Protocol, TypeIs, cast

from websockets.asyncio.client import ClientConnection

from app.core.json_types import JsonObject


def is_object_list(value: object) -> TypeIs[list[object]]:
    """Narrow a decoded payload value to a list whose items are still unchecked."""
    return isinstance(value, list)


def is_object_dict(value: object) -> TypeIs[dict[object, object]]:
    """Narrow a decoded payload value to a mapping whose keys and items are still unchecked."""
    return isinstance(value, dict)


class DingTalkCredential(Protocol):
    @property
    def client_id(self) -> str: ...

    @property
    def client_secret(self) -> str: ...


class DingTalkClient(Protocol):
    """The part of ``dingtalk_stream.DingTalkStreamClient`` that ``DingTalkTransport`` drives."""

    OPEN_CONNECTION_API: str

    @property
    def credential(self) -> DingTalkCredential: ...

    @property
    def callback_handler_map(self) -> Mapping[str, object]: ...

    @property
    def _is_event_required(self) -> bool: ...

    @property
    def websocket(self) -> object: ...

    @websocket.setter
    def websocket(self, value: ClientConnection, /) -> None: ...

    def pre_start(self) -> None: ...

    def get_host_ip(self) -> str: ...

    async def keepalive(self, ws: ClientConnection) -> None: ...

    async def background_task(self, json_message: JsonObject) -> None: ...


class DingTalkChatbotMessage(Protocol):
    """Fields of ``dingtalk_stream.ChatbotMessage`` the stream handler reads; DingTalk may omit any of them."""

    @property
    def sender_staff_id(self) -> str | None: ...

    @property
    def sender_id(self) -> str | None: ...

    @property
    def sender_nick(self) -> str | None: ...

    @property
    def message_id(self) -> str | None: ...

    @property
    def conversation_id(self) -> str | None: ...

    @property
    def conversation_type(self) -> str | None: ...

    @property
    def session_webhook(self) -> str | None: ...

    def get_text_list(self) -> Sequence[object] | None: ...


def dingtalk_chatbot_message(data: object) -> DingTalkChatbotMessage:
    """Parse a callback payload with the SDK; the import is lazy because ``dingtalk_stream`` is optional."""
    from dingtalk_stream import ChatbotMessage

    return cast("DingTalkChatbotMessage", ChatbotMessage.from_dict(data))
