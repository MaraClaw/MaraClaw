"""``DeleteObjects`` and ``CopyObject`` results as per-key storage results, shared by the plain and the metered backends.

A version check rides in the request itself (a per-object ``ETag`` on a delete, ``CopySourceIfMatch`` on a copy), so the
provider compares it at the moment it acts and a key whose object changed since it was listed is refused and left alone.
"""

from collections.abc import Collection, Mapping
from dataclasses import dataclass

from botocore.exceptions import ClientError

from app.core.json_types import is_str_dict
from app.services.storage_runtime.base import ConditionalWriteResult, StorageVersion, kept_result
from app.services.storage_runtime.s3_connection import response_version_id
from app.services.storage_runtime.s3_types import client_error_response, mapping_items

_PRECONDITION_FAILED = "PreconditionFailed"
_ABSENT_CODES = frozenset({"NoSuchKey", "NotFound", "404"})
_REFUSED_CODES = frozenset({"AccessDenied", "InvalidArgument", "InvalidRequest"})


def quoted_etag(etag: str) -> str:
    """An entity tag in the quoted form conditional requests compare; listings and HEADs report it unquoted."""
    return f'"{etag}"'


@dataclass(frozen=True, slots=True)
class DeleteVerdicts:
    """How ``DeleteObjects`` answered for the object keys it was sent.

    ``changed`` keys failed their ETag, ``absent`` keys were not there, ``refused`` keys met a definitive refusal; none of
    the three deleted anything. A key in no set got no usable answer and is unconfirmed.
    """

    deleted: frozenset[str]
    changed: frozenset[str]
    absent: frozenset[str]
    refused: frozenset[str]


def delete_verdicts(response: Mapping[str, object], sent: Collection[str]) -> DeleteVerdicts:
    answered = set(sent)
    codes = {
        key: str(item.get("Code") or "")
        for item in mapping_items(response.get("Errors"))
        if isinstance(key := item.get("Key"), str) and key in answered
    }
    return DeleteVerdicts(
        deleted=frozenset(
            key for item in mapping_items(response.get("Deleted")) if isinstance(key := item.get("Key"), str)
        )
        & answered,
        changed=frozenset(key for key, code in codes.items() if code == _PRECONDITION_FAILED),
        absent=frozenset(key for key, code in codes.items() if code in _ABSENT_CODES),
        refused=frozenset(key for key, code in codes.items() if code in _REFUSED_CODES),
    )


def copy_conflict(exc: ClientError, key: str) -> ConditionalWriteResult | None:
    """A source that changed (412) or is gone (404) is a conflict; any other failure is ``None`` and must propagate."""
    response = client_error_response(exc)
    code = response.get("Error", {}).get("Code", "")
    status = response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    if status == 412 or code == _PRECONDITION_FAILED:
        return kept_result(key, exists=True)
    if status == 404 or code in _ABSENT_CODES:
        return kept_result(key, exists=False)
    return None


def copied_result(destination_key: str, size: int, response: Mapping[str, object]) -> ConditionalWriteResult:
    details = response.get("CopyObjectResult")
    etag = str(details.get("ETag") or "").strip('"') if is_str_dict(details) else ""
    return ConditionalWriteResult(
        ok=True,
        current_version=StorageVersion(
            key=destination_key,
            exists=True,
            is_dir=False,
            size=size,
            etag=etag,
            version_id=response_version_id(response),
        ),
    )
