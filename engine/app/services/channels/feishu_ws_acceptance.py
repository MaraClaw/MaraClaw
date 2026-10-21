from collections.abc import Awaitable, Callable, Iterable
from typing import Protocol

from app.core.json_types import JsonObject, json_loads_object, json_object_from
from app.core.logging import logger


class FrameHeader(Protocol):
    key: str
    value: str


class EventFrame(Protocol):
    @property
    def headers(self) -> Iterable[FrameHeader]: ...

    payload: bytes
    SerializeToString: Callable[[], bytes]


class EventClient(Protocol):
    def _combine(self, message_id: str, total: int, sequence: int, payload: bytes, /) -> bytes | None: ...

    async def _write_message(self, payload: bytes, /) -> None: ...


async def accept_event_frame(
    client: EventClient, frame: EventFrame, accept: Callable[[JsonObject], Awaitable[None]]
) -> None:
    headers = {header.key: header.value for header in frame.headers}
    if headers.get("type") != "event":
        return
    payload = frame.payload
    total = int(headers.get("sum", "1"))
    if total > 1:
        combined = client._combine(headers["message_id"], total, int(headers["seq"]), payload)
        if combined is None:
            return
        payload = combined
    try:
        body = json_loads_object(payload)
        if json_object_from(body.get("header")).get("event_type") == "im.message.receive_v1":
            await accept(body)
    except Exception:
        logger.exception("[Feishu WS] Event acceptance failed")
        frame.payload = b'{"code":500}'
    else:
        frame.payload = b'{"code":200}'
    await client._write_message(frame.SerializeToString())
