"""Normalize anydoc Markdown and map its typed failures for inbound callers."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final, Literal, NoReturn, Protocol, cast

from app.core.logging import logger
from app.services.document_parser.errors import (
    DocumentParseError,
    DocumentParseUnavailableError,
    DocumentTooLargeError,
    EncryptedDocumentError,
    MalformedDocumentError,
    MissingDocumentPartError,
    ResourceLimitedDocumentError,
    UnsupportedDocumentError,
)

AnydocFormat = Literal["doc", "docx", "odt", "pdf", "ppt", "pptx", "rtf", "epub", "xlsx", "ods", "odp", "csv"]


class _AnydocModule(Protocol):
    def to_markdown_bytes(self, data: bytes, format: AnydocFormat | None = None) -> str: ...

    def format_from_bytes(self, data: bytes) -> AnydocFormat | None: ...

    def format_from_extension(self, extension: str) -> AnydocFormat | None: ...


# Ordinary text stays local. CSV is supported by anydoc but must not go through it here.
TEXT_EXTENSIONS: Final[set[str]] = {
    ".txt",
    ".md",
    ".markdown",
    ".csv",
    ".json",
    ".xml",
    ".yaml",
    ".yml",
    ".py",
    ".js",
    ".ts",
    ".html",
    ".htm",
    ".css",
    ".sql",
    ".sh",
    ".log",
    ".ini",
    ".cfg",
    ".conf",
    ".env",
    ".toml",
}

OFFICE_EXTENSIONS: Final[set[str]] = {
    ".doc",
    ".docx",
    ".docm",
    ".ppt",
    ".pps",
    ".pot",
    ".pptx",
    ".pptm",
    ".ppsx",
    ".ppsm",
    ".xls",
    ".xlsx",
    ".xlsm",
    ".xlsb",
    ".odt",
    ".ods",
    ".odp",
    ".rtf",
    ".epub",
    ".pdf",
}

_READ_DOCUMENT_SUPPORTED = "PDF, DOCX, XLSX, PPTX, TXT, MD, CSV"
_BLANK_LINE_RE = re.compile(r"\n{3,}")
MAX_INPUT_BYTES: Final = 50 * 1024 * 1024
MAX_OUTPUT_CHARS: Final = 2_000_000
_INSTALL_HINT = "Install: pip install firecrawl-anydoc"


def needs_extraction(filename: str) -> bool:
    """Return True when the filename is an inbound office document."""
    return Path(filename).suffix.lower() in OFFICE_EXTENSIONS


def is_plain_text_document(filename: str) -> bool:
    """Return True when callers should decode the file as local text."""
    return Path(filename).suffix.lower() in TEXT_EXTENSIONS


def decode_text_bytes(content: bytes) -> str:
    """Decode ordinary text without sending it through anydoc."""
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError:
        return content.decode("gbk", errors="replace")


def normalize_markdown(text: str) -> str:
    """Collapse anydoc Markdown into a stable UTF-8 string for every caller."""
    if not text:
        return ""
    cleaned = text.replace("\x00", "").replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip() for line in cleaned.split("\n")]
    collapsed = _BLANK_LINE_RE.sub("\n\n", "\n".join(lines))
    return collapsed.strip()


def format_parse_failure(exc: BaseException) -> str:
    """Stable caller-facing message for a typed parse failure."""
    if isinstance(exc, DocumentTooLargeError):
        return (
            f"Document is too large to read safely ({exc.size_bytes / 1024 / 1024:.1f} MB). "
            + "Please split or convert it to a smaller text/Markdown excerpt first."
        )
    if isinstance(exc, EncryptedDocumentError):
        return "Document is encrypted or password-protected."
    if isinstance(exc, ResourceLimitedDocumentError):
        limit = f" ({exc.limit})" if exc.limit else ""
        return f"Document exceeded a parser safety limit{limit}."
    if isinstance(exc, MissingDocumentPartError):
        part = f" ({exc.part})" if exc.part else ""
        return f"Document is missing a required part{part}."
    if isinstance(exc, MalformedDocumentError):
        return "Document is malformed and could not be converted."
    if isinstance(exc, DocumentParseUnavailableError):
        return f"Missing dependency: {exc}. {_INSTALL_HINT}"
    if isinstance(exc, UnsupportedDocumentError):
        detail = str(exc).lower()
        if "scanned" in detail or "ocr" in detail:
            return "This PDF looks scanned or image-only and needs OCR."
        return f"Unsupported file format. Supported: {_READ_DOCUMENT_SUPPORTED}"
    if isinstance(exc, DocumentParseError):
        return f"Document read failed: {str(exc)[:200]}"
    return f"Document read failed: {str(exc)[:200]}"


def unsupported_read_document_message(extension: str) -> str:
    return f"Unsupported file format: {extension}. Supported: {_READ_DOCUMENT_SUPPORTED}"


def convert_document(data: bytes, filename: str, *, max_output_chars: int | None = None) -> str:
    """Convert office-document bytes to normalized Markdown.

    Raises a typed ``DocumentParseError`` subclass. Does not spawn a process.
    """
    if len(data) > MAX_INPUT_BYTES:
        raise DocumentTooLargeError(
            f"Document is too large to read safely ({len(data) / 1024 / 1024:.1f} MB).",
            size_bytes=len(data),
        )
    anydoc = _import_anydoc()
    fmt = _format_hint(anydoc, data, filename)
    try:
        raw = anydoc.to_markdown_bytes(data, fmt)
    except Exception as exc:
        _reraise_typed(exc)
    markdown = normalize_markdown(raw)
    return _bound_output(markdown, max_output_chars)


def convert_document_path(path: Path, *, max_output_chars: int | None = None) -> str:
    """Convert a local office document path to normalized Markdown."""
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise DocumentParseError(f"Could not read document: {exc}") from exc
    return convert_document(data, path.name, max_output_chars=max_output_chars)


def extract_document_text(data: bytes, filename: str) -> str | None:
    """Best-effort inbound extract. Returns None when conversion is impossible."""
    if not needs_extraction(filename):
        return None
    try:
        markdown = convert_document(data, filename)
    except DocumentParseError as exc:
        logger.error(f"[DocumentParser] Failed to extract from {filename}: {exc}")
        return None
    return markdown or None


def save_extracted_markdown(save_path: Path, data: bytes, filename: str) -> Path | None:
    """Write a same-stem UTF-8 Markdown sidecar next to the original file."""
    text = extract_document_text(data, filename)
    if not text:
        return None
    md_path = save_path.parent / f"{save_path.stem}.md"
    _ = md_path.write_text(text, encoding="utf-8")
    logger.info(f"[DocumentParser] Extracted {len(text)} chars from {filename} -> {md_path.name}")
    return md_path


def reconstruct_parse_error(
    type_name: str,
    message: str,
    *,
    part: str | None = None,
    limit: str | None = None,
    size_bytes: int | None = None,
) -> DocumentParseError:
    """Rebuild a typed failure from an isolated worker payload."""
    if type_name == EncryptedDocumentError.__name__:
        return EncryptedDocumentError(message)
    if type_name == MalformedDocumentError.__name__:
        return MalformedDocumentError(message, part=part)
    if type_name == MissingDocumentPartError.__name__:
        return MissingDocumentPartError(message, part=part)
    if type_name == ResourceLimitedDocumentError.__name__:
        return ResourceLimitedDocumentError(message, limit=limit)
    if type_name == UnsupportedDocumentError.__name__:
        return UnsupportedDocumentError(message)
    if type_name == DocumentTooLargeError.__name__:
        return DocumentTooLargeError(message, size_bytes=size_bytes or 0)
    if type_name == DocumentParseUnavailableError.__name__:
        return DocumentParseUnavailableError(message)
    if type_name == "DocumentParseTimeoutError":
        from app.services.document_parser.errors import DocumentParseTimeoutError

        return DocumentParseTimeoutError(message)
    return DocumentParseError(message)


def _import_anydoc() -> _AnydocModule:
    try:
        import anydoc
    except ImportError as exc:
        raise DocumentParseUnavailableError(str(exc)) from exc
    return cast(_AnydocModule, anydoc)


def _format_hint(anydoc: _AnydocModule, data: bytes, filename: str) -> AnydocFormat | None:
    detected = anydoc.format_from_bytes(data)
    hinted = anydoc.format_from_extension(Path(filename).suffix)
    fmt = detected or hinted
    if fmt == "csv":
        raise UnsupportedDocumentError("CSV is decoded as local text, not via anydoc")
    return fmt


def _reraise_typed(exc: BaseException) -> NoReturn:
    try:
        import anydoc
    except ImportError:
        raise DocumentParseError(str(exc)) from exc
    if isinstance(exc, anydoc.EncryptedError):
        raise EncryptedDocumentError(str(exc)) from exc
    if isinstance(exc, anydoc.MalformedError):
        raise MalformedDocumentError(str(exc), part=exc.part) from exc
    if isinstance(exc, anydoc.MissingPartError):
        raise MissingDocumentPartError(str(exc), part=exc.part) from exc
    if isinstance(exc, anydoc.ResourceLimitError):
        raise ResourceLimitedDocumentError(str(exc), limit=exc.limit) from exc
    if isinstance(exc, anydoc.UnsupportedError):
        raise UnsupportedDocumentError(str(exc)) from exc
    if isinstance(exc, anydoc.ConvertError):
        raise DocumentParseError(str(exc)) from exc
    if isinstance(exc, OSError):
        raise DocumentParseError(f"Could not read document: {exc}") from exc
    if isinstance(exc, ValueError):
        raise UnsupportedDocumentError(str(exc)) from exc
    raise DocumentParseError(str(exc)) from exc


def _bound_output(markdown: str, max_output_chars: int | None) -> str:
    limit = MAX_OUTPUT_CHARS if max_output_chars is None else max(1, int(max_output_chars))
    if len(markdown) <= limit:
        return markdown
    return markdown[:limit] + f"\n\n...[truncated, {len(markdown)} chars total]"
