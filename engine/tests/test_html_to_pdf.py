import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.services.document_conversion.chrome_runtime import chrome_arguments
from app.services.document_conversion.html_to_pdf import convert_html_to_pdf


@pytest.mark.asyncio
async def test_convert_html_to_pdf_falls_back_when_chrome_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    # Given: no local Chrome and a usable WeasyPrint adapter
    source = tmp_path / "src.html"
    target = tmp_path / "tgt.pdf"
    source.write_text("<p>Hello</p>", encoding="utf-8")
    html = MagicMock()
    monkeypatch.setattr("app.services.document_conversion.html_to_pdf.chrome_executable", lambda: None)
    monkeypatch.setitem(sys.modules, "weasyprint", SimpleNamespace(HTML=html))

    # When: conversion is requested
    result = await convert_html_to_pdf(source, target, "tgt.pdf", {})

    # Then: the fallback renders without attempting a subprocess launch
    html.assert_called_once_with(filename=str(source))
    assert "WeasyPrint" in result


@pytest.mark.parametrize(
    ("platform", "expects_sandbox_flags"),
    [("linux", True), ("darwin", False)],
)
def test_chrome_arguments_preserve_platform_sandbox_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, platform: str, expects_sandbox_flags: bool
):
    # Given: a platform and a trusted Chrome executable
    monkeypatch.setattr("app.services.document_conversion.chrome_runtime.sys.platform", platform)

    # When: the fixed Chrome argv is built
    arguments = chrome_arguments(tmp_path / "chrome", 9222, str(tmp_path / "profile"))

    # Then: Linux gets the required container flags and other platforms do not
    assert ("--no-sandbox" in arguments) is expects_sandbox_flags
    assert ("--disable-setuid-sandbox" in arguments) is expects_sandbox_flags
