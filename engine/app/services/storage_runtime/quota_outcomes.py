import hashlib
from enum import StrEnum
from typing import Final

from botocore.exceptions import ClientError, ConnectTimeoutError, EndpointConnectionError

from app.core.thread_slots import SlotsBusyError
from app.services.storage_runtime.base import AtomicCreateUnsupportedError, StorageConditionalRaceError
from app.services.storage_runtime.s3_types import client_error_response


class Outcome(StrEnum):
    """What is known about one provider call once it has returned or raised."""

    LANDED = "landed"
    REJECTED = "rejected"
    UNKNOWN = "unknown"


_NEVER_SENT: Final = (
    EndpointConnectionError,
    ConnectTimeoutError,
    AtomicCreateUnsupportedError,
    StorageConditionalRaceError,
    SlotsBusyError,
)


def failure_outcome(exc: BaseException) -> Outcome:
    """``REJECTED`` only when the provider cannot have applied the request.

    A 4xx answer, a connection that never opened, a call the full storage lane refused before it started, an
    unsupported capability or an exhausted 409 conditional race all mean no change. Everything else - a 5xx, a
    timeout or reset after sending, cancellation, any unexpected error - may have been applied, so the reservation
    stays charged.
    """
    if isinstance(exc, ClientError):
        status = client_error_response(exc).get("ResponseMetadata", {}).get("HTTPStatusCode")
        return Outcome.REJECTED if isinstance(status, int) and 400 <= status < 500 else Outcome.UNKNOWN
    return Outcome.REJECTED if isinstance(exc, _NEVER_SENT) else Outcome.UNKNOWN


def content_md5(data: bytes) -> str:
    """Payload correlation for audit; a matching HEAD never authorizes releasing an uncertain operation."""
    return hashlib.md5(data, usedforsecurity=False).hexdigest()
