from contextlib import aclosing
from typing import Final

from starlette.requests import Request

CHANNEL_WEBHOOK_MAX_BODY_SIZE: Final = 1024 * 1024  # Callback metadata, not file uploads.


async def read_bounded_body(request: Request, max_bytes: int) -> bytes | None:
    """Return a cached, bounded body, or None without draining an oversized stream."""
    declared_length = request.headers.get("content-length")
    if declared_length is not None:
        try:
            if int(declared_length) > max_bytes:
                return None
        except ValueError:
            pass  # An invalid length never replaces the streaming limit.

    body = bytearray()
    async with aclosing(request.stream()) as stream:
        async for chunk in stream:
            if len(chunk) > max_bytes - len(body):
                return None
            body.extend(chunk)

    # Starlette's body()/json() must remain usable after consuming stream().
    request._body = bytes(body)
    return request._body
