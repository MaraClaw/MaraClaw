"""Blocking S3 response ownership, including cancellation of the awaiter."""

from collections.abc import Callable, Mapping
from contextlib import closing
from typing import Protocol, TypeIs

from botocore.exceptions import ClientError

from app.services.storage_runtime.bounded import read_bounded


class _ResponseBody(Protocol):
    def read(self, amt: int | None = None) -> bytes: ...

    def close(self) -> None: ...


def _is_response_body(value: object) -> TypeIs[_ResponseBody]:
    return callable(getattr(value, "read", None)) and callable(getattr(value, "close", None))


def read_object(get_object: Callable[[], Mapping[str, object]], max_bytes: int | None) -> bytes:
    """Own GET, read and close in one worker, even after the awaiter abandons it.

    Cancellation cannot interrupt a blocking SDK call. The worker completes the
    bounded read (subject to the client's read timeout) and always closes its body.
    """
    try:
        response = get_object()
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") in {"404", "NoSuchKey", "NotFound"}:
            raise FileNotFoundError from exc
        raise
    body = response["Body"]
    if not _is_response_body(body):
        raise TypeError("S3 get_object response is missing a readable, closable Body")
    with closing(body):
        if max_bytes is None:
            return body.read()
        return read_bounded(body, max_bytes)
