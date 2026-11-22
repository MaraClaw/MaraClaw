"""Typed dictionary views of the unvalidated Google Chat event payload."""

from typing import NotRequired, TypeIs

from typing_extensions import TypedDict

from app.core.json_types import JsonValue


class GoogleChatEventPayload(TypedDict, extra_items=JsonValue):
    type: NotRequired[JsonValue]
    message: NotRequired[JsonValue]
    space: NotRequired[JsonValue]
    user: NotRequired[JsonValue]


class GoogleChatMessagePayload(TypedDict, total=False, extra_items=JsonValue):
    name: JsonValue
    space: JsonValue
    sender: JsonValue
    argumentText: JsonValue
    text: JsonValue
    annotations: JsonValue
    thread: JsonValue
    attachment: JsonValue
    attachments: JsonValue


class GoogleChatSpacePayload(TypedDict, total=False, extra_items=JsonValue):
    name: JsonValue
    type: JsonValue
    spaceType: JsonValue
    displayName: JsonValue


class GoogleChatUserPayload(TypedDict, total=False, extra_items=JsonValue):
    name: JsonValue
    type: JsonValue
    displayName: JsonValue
    email: JsonValue


class GoogleChatThreadPayload(TypedDict, total=False, extra_items=JsonValue):
    name: JsonValue


class GoogleChatAnnotationPayload(TypedDict, total=False, extra_items=JsonValue):
    type: JsonValue
    startIndex: JsonValue
    length: JsonValue


def is_chat_payload[
    Payload: GoogleChatMessagePayload
    | GoogleChatSpacePayload
    | GoogleChatUserPayload
    | GoogleChatThreadPayload
    | GoogleChatAnnotationPayload
](value: JsonValue, _shape: type[Payload]) -> TypeIs[Payload]:
    """The views name optional keys; their contents still need the caller's checks."""
    return isinstance(value, dict)
