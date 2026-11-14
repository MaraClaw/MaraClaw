"""Payment-evidence files: magic-byte sniffing, safe names and the storage key.

The client's ``Content-Type`` and extension are never trusted: the stored type and extension come from the bytes.
"""

import hashlib
import unicodedata
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Final
from urllib.parse import quote
from uuid import UUID, uuid4

MAX_EVIDENCE_BYTES: Final = 10 * 1024 * 1024
STORAGE_PREFIX: Final = "_capacity_payments"
MAX_FILENAME_LENGTH: Final = 255
DEFAULT_STEM: Final = "payment-evidence"


@dataclass(frozen=True, slots=True)
class EvidenceKind:
    content_type: str
    extension: str


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


def storage_key(tenant_id: UUID, order_id: UUID, kind: EvidenceKind, token: UUID | None = None) -> str:
    """Key outside every agent prefix, so the managed per-agent storage quota never sees it."""
    return f"{STORAGE_PREFIX}/{tenant_id}/{order_id}/{token or uuid4()}.{kind.extension}"


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
