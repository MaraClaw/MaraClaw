"""Typed inbound document-parse failures. Callers should not import `anydoc`."""

from __future__ import annotations


class DocumentParseError(Exception):
    """Meaningful conversion was impossible."""


class UnsupportedDocumentError(DocumentParseError):
    """Unknown format, or one that cannot be converted (an image-only PDF)."""


class MalformedDocumentError(DocumentParseError):
    """Structurally unusable: no meaningful content could be extracted."""

    def __init__(self, message: str, *, part: str | None = None) -> None:
        super().__init__(message)
        self.part: str | None = part


class EncryptedDocumentError(DocumentParseError):
    """Encrypted or password-protected."""


class ResourceLimitedDocumentError(DocumentParseError):
    """A fixed safety limit was crossed (decompression, nesting, expansion)."""

    def __init__(self, message: str, *, limit: str | None = None) -> None:
        super().__init__(message)
        self.limit: str | None = limit


class MissingDocumentPartError(DocumentParseError):
    """A part required for any meaningful output is absent."""

    def __init__(self, message: str, *, part: str | None = None) -> None:
        super().__init__(message)
        self.part: str | None = part


class DocumentTooLargeError(DocumentParseError):
    """The input bytes exceeded the inbound parse budget."""

    def __init__(self, message: str, *, size_bytes: int) -> None:
        super().__init__(message)
        self.size_bytes: int = size_bytes


class DocumentParseTimeoutError(DocumentParseError):
    """The isolated parser did not finish within the time budget."""


class DocumentParseUnavailableError(DocumentParseError):
    """The local anydoc binding is not installed."""
