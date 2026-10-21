"""HTML to PPTX conversion service."""

from collections.abc import Mapping
from pathlib import Path

from app.services.document_conversion.pptx_renderer import render_html_to_pptx


async def convert_html_to_pptx(
    src_file: Path, tgt_file: Path, target_path: str, ws: Path, arguments: Mapping[str, object]
) -> str:
    return await render_html_to_pptx(src_file, tgt_file, target_path, ws, arguments)
