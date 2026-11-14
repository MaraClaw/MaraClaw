"""Magic-byte sniffing, safe names and storage keys of payment evidence."""

from uuid import UUID

import pytest

from app.services.capacity_evidence import (
    JPEG,
    MAX_EVIDENCE_BYTES,
    PDF,
    PNG,
    STORAGE_PREFIX,
    WEBP,
    EvidenceKind,
    content_disposition,
    safe_filename,
    sha256_hex,
    sniff_evidence,
    storage_key,
)

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
JPEG_BYTES = b"\xff\xd8\xff\xe0" + b"\x00" * 16
WEBP_BYTES = b"RIFF\x24\x00\x00\x00WEBPVP8 " + b"\x00" * 16
PDF_BYTES = b"%PDF-1.7\n" + b"\x00" * 16


@pytest.mark.parametrize(
    ("data", "kind"),
    [(PNG_BYTES, PNG), (JPEG_BYTES, JPEG), (WEBP_BYTES, WEBP), (PDF_BYTES, PDF)],
)
def test_each_allowed_type_is_recognized_from_its_leading_bytes(data: bytes, kind: EvidenceKind):
    assert sniff_evidence(data) == kind


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"GIF89a" + b"\x00" * 16,
        b"<html><script>alert(1)</script></html>",
        b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\x00" * 16,
        b"RIFF\x24\x00",
        b"  %PDF-1.7",
        b"\x89PNG",
        b"MZ\x90\x00\x03\x00\x00\x00",
    ],
)
def test_everything_else_is_refused(data: bytes):
    assert sniff_evidence(data) is None


def test_a_type_is_decided_by_the_bytes_whatever_the_client_calls_the_file():
    # A script named like an image, and an image named like a script: only the bytes count.
    assert sniff_evidence(b"<script>alert(1)</script>") is None
    assert safe_filename("receipt.exe", sniff_evidence(PNG_BYTES) or PNG) == "receipt.png"
    assert safe_filename("scan.html", PDF) == "scan.pdf"


@pytest.mark.parametrize(
    ("client_name", "expected"),
    [
        ("bank-receipt.PNG", "bank-receipt.png"),
        ("../../etc/passwd", "passwd.png"),
        ("..\\..\\windows\\evil.exe", "evil.png"),
        ("C:\\Users\\a\\transfer 03.jpg", "transfer 03.png"),
        ("bi\x00ll\r\ning\x1b.png", "billing.png"),
        ("   ", "payment-evidence.png"),
        ("...", "payment-evidence.png"),
        ("", "payment-evidence.png"),
        (None, "payment-evidence.png"),
        ("chuyển khoản.png", "chuyển khoản.png"),
    ],
)
def test_the_client_file_name_is_reduced_to_one_printable_segment(client_name: str | None, expected: str):
    assert safe_filename(client_name, PNG) == expected


def test_a_very_long_name_fits_the_column():
    name = safe_filename("a" * 1000 + ".pdf", PDF)
    assert len(name) == 255
    assert name.endswith(".pdf")


@pytest.mark.parametrize("hostile", ['a"; filename=evil.exe', "x\r\nSet-Cookie: s=1", "a\\b", "über.png"])
def test_the_content_disposition_cannot_break_out_of_its_header(hostile: str):
    header = content_disposition(hostile)
    assert header.startswith('inline; filename="')
    assert "\r" not in header
    assert "\n" not in header
    plain = header.split('"')[1]
    assert plain.isascii()
    assert all(char.isalnum() or char in "._- " for char in plain)
    assert "filename*=UTF-8''" in header


def test_the_storage_key_sits_outside_every_agent_prefix_and_is_unique_per_attempt():
    tenant, order = UUID(int=1), UUID(int=2)
    first, second = storage_key(tenant, order, PDF), storage_key(tenant, order, PDF)
    assert first != second
    assert first.startswith(f"{STORAGE_PREFIX}/{tenant}/{order}/")
    assert first.endswith(".pdf")
    assert STORAGE_PREFIX.startswith("_")
    with pytest.raises(ValueError, match="badly formed"):
        _ = UUID(first.split("/", maxsplit=1)[0])


def test_the_limit_is_ten_mebibytes_and_the_digest_is_sha256():
    assert MAX_EVIDENCE_BYTES == 10 * 1024 * 1024
    assert sha256_hex(b"abc") == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
