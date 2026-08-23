from __future__ import annotations

from pathlib import Path

import pytest

from app.services.document_parser import (
    MAX_INPUT_BYTES,
    EncryptedDocumentError,
    MissingDocumentPartError,
    ResourceLimitedDocumentError,
    UnsupportedDocumentError,
    convert_document,
    convert_document_isolated,
    decode_text_bytes,
    extract_document_text,
    format_parse_failure,
    is_plain_text_document,
    needs_extraction,
    normalize_markdown,
    preview_kind,
    save_extracted_markdown,
)
from app.services.document_parser.errors import DocumentParseError, DocumentParseTimeoutError, DocumentTooLargeError
from app.services.document_parser.runtime import _CONVERT_PERMITS, _error_from_payload, convert_worker

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "anydoc"


def _fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def test_needs_extraction_covers_office_but_not_local_text() -> None:
    assert needs_extraction("report.docx")
    assert needs_extraction("deck.pptx")
    assert needs_extraction("sheet.xlsx")
    assert needs_extraction("notes.pdf")
    assert not needs_extraction("notes.csv")
    assert not needs_extraction("readme.md")
    assert is_plain_text_document("notes.csv")
    assert decode_text_bytes(b"hello") == "hello"
    assert preview_kind("notes.odt") == "docx"
    assert preview_kind("sheet.ods") == "docx"
    assert preview_kind("legacy.xls") == "xlsx"


def test_decode_text_bytes_when_utf8_with_invalid_byte_then_keeps_cjk() -> None:
    payload = "Quarterly Report 你好 Ada".encode() + b"\xff"
    text = decode_text_bytes(payload)
    assert "你好" in text
    assert "浣犲ソ" not in text


def test_decode_text_bytes_when_gbk_then_decodes() -> None:
    assert decode_text_bytes("你好".encode("gbk")) == "你好"


def test_decode_text_bytes_when_cp1252_then_does_not_gbk_reinterpret() -> None:
    text = decode_text_bytes(b"Hello \x93world\x94")
    assert text.startswith("Hello ")
    assert "搘orld" not in text


def test_normalize_markdown_collapses_blank_lines_and_nulls() -> None:
    assert normalize_markdown("a\r\n\r\n\r\n\x00b  \n\n") == "a\n\nb"


@pytest.mark.parametrize(
    ("name", "needle"),
    [
        ("supported.docx", "Quarterly Report"),
        ("supported.pptx", "Hello from PPTX"),
        ("supported.xlsx", "Revenue"),
        ("supported.pdf", "Hello from PDF"),
    ],
)
def test_convert_document_when_supported_then_returns_normalized_markdown(name: str, needle: str) -> None:
    markdown = convert_document(_fixture(name), name)
    assert needle in markdown
    assert "\x00" not in markdown


def test_convert_document_when_malformed_then_raises_typed_error() -> None:
    with pytest.raises(MissingDocumentPartError) as exc:
        convert_document(_fixture("malformed.docx"), "malformed.docx")
    assert exc.value.part == "word/document.xml"
    assert "missing a required part" in format_parse_failure(exc.value)


def test_convert_document_when_encrypted_then_raises_typed_error() -> None:
    with pytest.raises(EncryptedDocumentError):
        convert_document(_fixture("encrypted.odt"), "encrypted.odt")
    assert format_parse_failure(EncryptedDocumentError("document is encrypted")) == (
        "Document is encrypted or password-protected."
    )


def test_convert_document_when_resource_limited_then_raises_typed_error() -> None:
    with pytest.raises(ResourceLimitedDocumentError) as exc:
        convert_document(_fixture("resource_limited.ods"), "resource_limited.ods")
    assert exc.value.limit == "max_expansion"
    assert "safety limit" in format_parse_failure(exc.value)


def test_convert_document_when_scanned_pdf_then_raises_unsupported() -> None:
    with pytest.raises(UnsupportedDocumentError) as exc:
        convert_document(_fixture("scanned.pdf"), "scanned.pdf")
    assert "scanned" in str(exc.value).lower()
    assert "OCR" in format_parse_failure(exc.value)


def test_convert_document_when_csv_then_stays_unsupported_for_anydoc() -> None:
    with pytest.raises(UnsupportedDocumentError):
        convert_document(b"a,b\n1,2\n", "sheet.csv")


def test_extract_and_sidecar_when_supported_then_write_same_stem_utf8(tmp_path: Path) -> None:
    data = _fixture("supported.docx")
    original = tmp_path / "report.docx"
    original.write_bytes(data)

    text = extract_document_text(data, "report.docx")
    assert text is not None
    assert "Ada" in text

    sidecar = save_extracted_markdown(original, data, "report.docx")
    assert sidecar == tmp_path / "report.md"
    assert sidecar is not None
    written = sidecar.read_text(encoding="utf-8")
    assert written == text
    assert original.exists()


def test_extract_when_encrypted_then_returns_none() -> None:
    assert extract_document_text(_fixture("encrypted.odt"), "secret.odt") is None


def test_convert_document_when_too_large_then_rejects_before_anydoc(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.services.document_parser.service.MAX_INPUT_BYTES", 8)
    with pytest.raises(DocumentTooLargeError) as exc:
        convert_document(b"0123456789", "big.pdf")
    assert exc.value.size_bytes == 10
    assert "too large to read safely" in format_parse_failure(exc.value)


def test_convert_document_isolated_when_supported_then_matches_in_process() -> None:
    data = _fixture("supported.xlsx")
    assert convert_document_isolated(data, "supported.xlsx") == convert_document(data, "supported.xlsx")


def test_convert_document_isolated_when_worker_hangs_then_times_out(monkeypatch: pytest.MonkeyPatch) -> None:
    class HangingProcess:
        exitcode = None
        pid = None
        terminated = False
        killed = False

        def start(self) -> None:
            return None

        def join(self, _: float) -> None:
            return None

        def is_alive(self) -> bool:
            return not (self.terminated or self.killed)

        def terminate(self) -> None:
            self.terminated = True

        def kill(self) -> None:
            self.killed = True

    class FakeContext:
        def Process(self, **_: object) -> HangingProcess:  # noqa: N802
            return HangingProcess()

    monkeypatch.setattr("app.services.document_parser.runtime.mp.get_context", lambda _: FakeContext())
    with pytest.raises(DocumentParseTimeoutError):
        convert_document_isolated(_fixture("supported.pdf"), "supported.pdf", timeout_seconds=0.2)


def test_convert_document_isolated_when_slots_busy_then_times_out_waiting() -> None:
    assert _CONVERT_PERMITS.acquire(timeout=0)
    assert _CONVERT_PERMITS.acquire(timeout=0)
    try:
        with pytest.raises(DocumentParseTimeoutError, match="parser slot"):
            convert_document_isolated(b"%PDF-1.4", "a.pdf", slot_wait_seconds=0.05)
    finally:
        _CONVERT_PERMITS.release()
        _CONVERT_PERMITS.release()


def test_error_from_payload_when_invalid_json_then_typed_parse_error() -> None:
    error = _error_from_payload("{not json")
    assert isinstance(error, DocumentParseError)
    assert "unreadable error" in str(error)


def test_convert_worker_when_parse_fails_then_writes_typed_payload(tmp_path: Path) -> None:
    src = tmp_path / "in.bin"
    out = tmp_path / "out.md"
    err = tmp_path / "err.json"
    src.write_bytes(_fixture("encrypted.odt"))
    convert_worker(str(src), "encrypted.odt", str(out), str(err), None)
    assert not out.exists()
    assert "EncryptedDocumentError" in err.read_text(encoding="utf-8")


def test_max_input_budget_matches_read_document_ceiling() -> None:
    assert MAX_INPUT_BYTES == 50 * 1024 * 1024


def test_pyproject_pins_published_firecrawl_anydoc() -> None:
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    dockerfile = Path(__file__).resolve().parents[1] / "Dockerfile"
    assert "firecrawl-anydoc==0.2.3" in pyproject.read_text(encoding="utf-8")
    dockerfile_text = dockerfile.read_text(encoding="utf-8")
    assert "FROM python:3.14.7-slim-trixie AS production" in dockerfile_text
    assert "python /app/scripts/verify_anydoc.py" in dockerfile_text
