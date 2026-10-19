from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
from typing import Final

from app.core.json_types import json_loads_object, json_loads_value, object_mapping_from
from app.core.logging import logger
from app.services.document_parser.errors import (
    DocumentParseError,
    DocumentTooLargeError,
    MalformedDocumentError,
    MissingDocumentPartError,
    ResourceLimitedDocumentError,
)
from app.services.document_parser.service import convert_document, extract_xlsx_sheets, reconstruct_parse_error

TIMEOUT_SECONDS: Final = 25
_CHILD_AS_BYTES: Final = 768 * 1024 * 1024
_CHILD_FSIZE_BYTES: Final = 64 * 1024 * 1024


def convert_worker(
    src_path: str,
    filename: str,
    out_path: str,
    err_path: str,
    max_output_chars: int | None,
) -> None:
    """Spawn entrypoint. Must stay a module-level function so it pickles."""
    _harden_child()
    try:
        markdown = convert_document(Path(src_path).read_bytes(), filename, max_output_chars=max_output_chars)
        _ = Path(out_path).write_text(markdown, encoding="utf-8")
    except BaseException as exc:
        _write_worker_error(err_path, exc)


def xlsx_sheets_worker(
    src_path: str,
    filename: str,
    out_path: str,
    err_path: str,
    _: object,
) -> None:
    """Spawn entrypoint for structured XLSX preview. Must stay module-level."""
    del filename
    _harden_child()
    try:
        sheets = extract_xlsx_sheets(Path(src_path).read_bytes())
        _ = Path(out_path).write_text(json.dumps(sheets), encoding="utf-8")
    except BaseException as exc:
        _write_worker_error(err_path, exc)


def _harden_child() -> None:
    with contextlib.suppress(OSError):
        os.setsid()
    try:
        import resource

        cpu = TIMEOUT_SECONDS + 5
        resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))
        resource.setrlimit(resource.RLIMIT_AS, (_CHILD_AS_BYTES, _CHILD_AS_BYTES))
        resource.setrlimit(resource.RLIMIT_FSIZE, (_CHILD_FSIZE_BYTES, _CHILD_FSIZE_BYTES))
        if hasattr(resource, "RLIMIT_CORE"):
            resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    except Exception as exc:
        logger.warning(f"[DocumentParser] Failed to apply child resource limits: {exc}")


def _write_worker_error(err_path: str, exc: BaseException) -> None:
    payload = {
        "type": type(exc).__name__,
        "message": str(exc)[:2000],
        "part": exc.part if isinstance(exc, (MalformedDocumentError, MissingDocumentPartError)) else None,
        "limit": exc.limit if isinstance(exc, ResourceLimitedDocumentError) else None,
        "size_bytes": exc.size_bytes if isinstance(exc, DocumentTooLargeError) else None,
    }
    try:
        _ = Path(err_path).write_text(json.dumps(payload), encoding="utf-8")
    except OSError:
        raise SystemExit(1) from exc


def decode_sheets(raw: str) -> list[dict[str, object]]:
    try:
        payload = json_loads_value(raw)
    except json.JSONDecodeError as exc:
        raise DocumentParseError("Document read failed: extractor returned unreadable sheets") from exc
    if not isinstance(payload, list):
        raise DocumentParseError("Document read failed: extractor returned unreadable sheets")
    return [object_mapping_from(item) for item in list[object](payload) if isinstance(item, dict)]


def decode_worker_error(raw: str) -> DocumentParseError:
    try:
        payload = json_loads_object(raw)
    except Exception:
        return DocumentParseError("Document read failed: extractor returned unreadable error")
    if not payload:
        return DocumentParseError("Document read failed: extractor returned unreadable error")
    type_name = payload.get("type")
    message = payload.get("message")
    part = payload.get("part")
    limit = payload.get("limit")
    size_bytes = payload.get("size_bytes")
    return reconstruct_parse_error(
        type_name if isinstance(type_name, str) else DocumentParseError.__name__,
        message if isinstance(message, str) else "Document read failed",
        part=part if isinstance(part, str) else None,
        limit=limit if isinstance(limit, str) else None,
        size_bytes=size_bytes if isinstance(size_bytes, int) else None,
    )
