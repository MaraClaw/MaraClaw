from __future__ import annotations

import importlib
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Protocol

_READ_DOCUMENT_MAX_FILE_BYTES = 50 * 1024 * 1024
_READ_DOCUMENT_MAX_CELL_CHARS = 500
_READ_DOCUMENT_MAX_COLUMNS = 80
_READ_DOCUMENT_MAX_XLSX_CELLS = 20000


class _DocumentCell(Protocol):
    text: str


class _DocumentRow(Protocol):
    @property
    def cells(self) -> Sequence[_DocumentCell]: ...


class _DocumentTable(Protocol):
    @property
    def rows(self) -> Iterable[_DocumentRow]: ...


class _DocumentParagraph(Protocol):
    text: str


def _safe_document_cell_text(value: object) -> str:
    """Convert spreadsheet/table values without letting pathological cells dominate CPU."""
    if value is None:
        return ""
    if isinstance(value, int) and value.bit_length() > 4096:
        return "[large integer omitted]"
    text = str(value)
    if len(text) > _READ_DOCUMENT_MAX_CELL_CHARS:
        return text[:_READ_DOCUMENT_MAX_CELL_CHARS] + "...[cell truncated]"
    return text


def _read_document_sync(ws: Path, rel_path: str, max_chars: int = 8000, tenant_id: str | None = None) -> str:
    """Synchronous document extraction. Must run outside the uvicorn event loop."""
    from app.services import agent_tools

    max_chars = min(max(int(max_chars), 1), 20000)
    try:
        file_path = agent_tools._resolve_tool_source_path(ws, rel_path, tenant_id=tenant_id)
    except ValueError as exc:
        return str(exc)

    if not file_path.exists():
        return f"File not found: {rel_path}"
    if file_path.is_dir():
        return f"Path is a directory, not a document: {rel_path}"
    try:
        file_size = file_path.stat().st_size
    except OSError:
        file_size = 0
    if file_size > _READ_DOCUMENT_MAX_FILE_BYTES:
        return (
            f"Document is too large to read safely ({file_size / 1024 / 1024:.1f} MB). "
            "Please split or convert it to a smaller text/Markdown excerpt first."
        )

    ext = file_path.suffix.lower()
    try:
        if ext == ".pdf":
            pdfplumber = importlib.import_module("pdfplumber")

            text_parts = []
            with pdfplumber.open(str(file_path)) as pdf:
                for index, page in enumerate(pdf.pages[:50]):
                    page_text = page.extract_text() or ""
                    if page_text:
                        text_parts.append(f"--- Page {index + 1} ---\n{page_text}")
                    if sum(len(part) for part in text_parts) >= max_chars:
                        break
            content = "\n\n".join(text_parts) if text_parts else "(PDF is empty or text extraction failed)"
        elif ext == ".docx":
            document_module = importlib.import_module("docx")
            document_xml_namespace = importlib.import_module("docx.oxml.ns")

            doc = document_module.Document(str(file_path))
            lines: list[str] = []

            def extract_para_text(para: _DocumentParagraph) -> str:
                return para.text.strip()

            def extract_table(table: _DocumentTable) -> str:
                rows = []
                for row in table.rows:
                    cells = [
                        _safe_document_cell_text(cell.text).strip() for cell in row.cells[:_READ_DOCUMENT_MAX_COLUMNS]
                    ]
                    if not cells:
                        continue
                    deduped = [cells[0]] + [cell for index, cell in enumerate(cells[1:]) if cell != cells[index]]
                    row_text = " | ".join(cell for cell in deduped if cell)
                    if row_text:
                        rows.append(row_text)
                return "\n".join(rows)

            for para in doc.paragraphs:
                text = extract_para_text(para)
                if text:
                    lines.append(text)
            for table in doc.tables:
                text = extract_table(table)
                if text:
                    lines.append(text)
            for shape in doc.element.body.iter(document_xml_namespace.qn("w:txbxContent")):
                lines.extend(
                    child.text.strip()
                    for child in shape.iter(document_xml_namespace.qn("w:t"))
                    if child.text and child.text.strip()
                )
            for section in doc.sections:
                for header_footer in [section.header, section.footer]:
                    if header_footer and header_footer.is_linked_to_previous is False:
                        for para in header_footer.paragraphs:
                            text = para.text.strip()
                            if text:
                                lines.append(text)
            content = "\n".join(lines) if lines else "(Document is empty or uses unsupported formatting)"
        elif ext == ".xlsx":
            openpyxl = importlib.import_module("openpyxl")

            workbook = openpyxl.load_workbook(str(file_path), read_only=True, data_only=True)
            sheets = []
            cell_count = 0
            for ws_name in workbook.sheetnames[:10]:
                sheet = workbook[ws_name]
                rows = []
                for row in sheet.iter_rows(max_row=200, max_col=_READ_DOCUMENT_MAX_COLUMNS, values_only=True):
                    visible = row
                    cell_count += len(visible)
                    if cell_count > _READ_DOCUMENT_MAX_XLSX_CELLS:
                        rows.append("[cell limit reached; remaining cells omitted]")
                        break
                    row_text = "\t".join(_safe_document_cell_text(cell) for cell in visible)
                    if row_text.strip():
                        rows.append(row_text)
                if rows:
                    sheets.append(f"=== Sheet: {ws_name} ===\n" + "\n".join(rows))
                if cell_count > _READ_DOCUMENT_MAX_XLSX_CELLS or sum(len(part) for part in sheets) >= max_chars:
                    break
            workbook.close()
            content = "\n\n".join(sheets) if sheets else "(Excel is empty)"
        elif ext == ".pptx":
            pptx = importlib.import_module("pptx")

            presentation = pptx.Presentation(str(file_path))
            slides = []
            for index, slide in enumerate(presentation.slides[:50]):
                texts = [shape.text for shape in slide.shapes if hasattr(shape, "text") and shape.text.strip()]
                if texts:
                    slides.append(f"--- Slide {index + 1} ---\n" + "\n".join(texts))
            content = "\n\n".join(slides) if slides else "(PPT is empty)"
        elif ext in (".txt", ".md", ".json", ".csv", ".log"):
            content = file_path.read_text(encoding="utf-8", errors="replace")
        else:
            return f"Unsupported file format: {ext}. Supported: PDF, DOCX, XLSX, PPTX, TXT, MD, CSV"

        if len(content) > max_chars:
            content = content[:max_chars] + f"\n\n...[truncated, {len(content)} chars total]"
        return content
    except ImportError as exc:
        return f"Missing dependency: {exc}. Install: pip install pdfplumber python-docx openpyxl python-pptx"
    except Exception as exc:
        return f"Document read failed: {str(exc)[:200]}"
