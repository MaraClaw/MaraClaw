"""Payment-evidence files: magic-byte sniffing, safe names, storage keys and the store/discard of the bytes.

Shared by capacity orders and organization admission requests. The client's ``Content-Type`` and extension are never
trusted: the stored type and extension come from the bytes.
"""

import hashlib
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Final
from urllib.parse import quote
from uuid import UUID, uuid4

from fastapi import HTTPException, Response

from app.core.logging import logger
from app.services.storage_runtime.base import StorageBackend

MAX_EVIDENCE_BYTES: Final = 10 * 1024 * 1024
STORAGE_PREFIX: Final = "_capacity_payments"
ADMISSION_STORAGE_PREFIX: Final = "_organization_admissions"
MAX_FILENAME_LENGTH: Final = 255
DEFAULT_STEM: Final = "payment-evidence"
EVIDENCE_WRITE_ATTEMPTS: Final = 3


@dataclass(frozen=True, slots=True)
class EvidenceKind:
    content_type: str
    extension: str


@dataclass(frozen=True, slots=True)
class EvidenceFile:
    data: bytes
    content_type: str
    filename: str


PNG: Final = EvidenceKind("image/png", "png")
JPEG: Final = EvidenceKind("image/jpeg", "jpg")
WEBP: Final = EvidenceKind("image/webp", "webp")
PDF: Final = EvidenceKind("application/pdf", "pdf")


def sniff_evidence(data: bytes) -> EvidenceKind | None:
    """The file type named by the leading bytes, or ``None`` when it is not a PNG, JPEG, WebP or PDF."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return PNG
    if data.startswith(b"\xff\xd8\xff"):
        return JPEG
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return WEBP
    if data.startswith(b"%PDF-"):
        return PDF
    return None


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _evidence_key(directory: str, kind: EvidenceKind, token: UUID | None) -> str:
    return f"{directory}/{token or uuid4()}.{kind.extension}"


def storage_key(tenant_id: UUID, order_id: UUID, kind: EvidenceKind, token: UUID | None = None) -> str:
    """Key outside every agent prefix, so the managed per-agent storage quota never sees it."""
    return _evidence_key(f"{STORAGE_PREFIX}/{tenant_id}/{order_id}", kind, token)


def admission_storage_key(request_id: UUID, kind: EvidenceKind, token: UUID | None = None) -> str:
    """Key of an organization admission's evidence, outside every agent prefix like the capacity keys."""
    return _evidence_key(f"{ADMISSION_STORAGE_PREFIX}/{request_id}", kind, token)


async def store_evidence(storage: StorageBackend, new_key: Callable[[], str], kind: EvidenceKind, data: bytes) -> str:
    """Write ``data`` under a fresh key from ``new_key`` (retrying a taken key) and return it."""
    for _ in range(EVIDENCE_WRITE_ATTEMPTS):
        key = new_key()
        written = await storage.create_bytes_if_absent(key, data, kind.content_type)
        if written.ok:
            return key
    raise HTTPException(503, "Could not store the payment evidence; try again")


async def discard_evidence(storage: StorageBackend, key: str) -> None:
    """Remove evidence that no row references any more; a failure is logged, never raised."""
    try:
        await storage.delete(key)
    except Exception as exc:
        logger.bind(exception_type=type(exc).__name__).warning("[payment-evidence] could not remove unused {}", key)


def safe_filename(client_name: str | None, kind: EvidenceKind) -> str:
    """The client's file name reduced to a single printable segment, with the extension of the sniffed type."""
    raw = (client_name or "").replace("\\", "/")
    stem = PurePosixPath(raw).stem if raw else ""
    printable = "".join(char for char in stem if not unicodedata.category(char).startswith("C")).strip(" .")
    room = MAX_FILENAME_LENGTH - len(kind.extension) - 1
    return f"{(printable or DEFAULT_STEM)[:room]}.{kind.extension}"


def content_disposition(filename: str) -> str:
    """An ``inline`` header value whose plain and RFC 5987 names cannot break out of the header."""
    ascii_name = "".join(char if char.isascii() and (char.isalnum() or char in "._- ") else "_" for char in filename)
    return f"inline; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename, safe='')}"


def evidence_response(evidence: EvidenceFile) -> Response:
    """The stored evidence with its sniffed type; sandboxed and never sniffed again by the browser."""
    return Response(
        evidence.data,
        media_type=evidence.content_type,
        headers={
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "sandbox",
            "Content-Disposition": content_disposition(evidence.filename),
            "Cache-Control": "private, no-store",
        },
    )
