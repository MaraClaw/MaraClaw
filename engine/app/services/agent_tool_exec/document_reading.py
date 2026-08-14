from __future__ import annotations

import importlib
from collections.abc import Iterable, Iterator, Sequence
from pathlib import Path
from typing import Protocol, TypeIs

from app.core.json_types import object_attr

_READ_DOCUMENT_MAX_FILE_BYTES = 50 * 1024 * 1024
_READ_DOCUMENT_MAX_CELL_CHARS = 500
_READ_DOCUMENT_MAX_COLUMNS = 80
_READ_DOCUMENT_MAX_XLSX_CELLS = 20000
_READ_DOCUMENT_INSTALL_HINT = "Install: pip install pdfplumber python-docx openpyxl python-pptx"


class _ContextManager(Protocol):
    def __enter__(self) -> object: ...

    def __exit__(self, *args: object) -> object: ...


class _PdfPlumberModule(Protocol):
    def open(self, path: str) -> _ContextManager: ...


class _DocxModule(Protocol):
    def Document(self, path: str) -> object: ...


class _DocxNsModule(Protocol):
    def qn(self, name: str) -> object: ...


class _Workbook(Protocol):
    sheetnames: object

    def __getitem__(self, name: str) -> object: ...

    def close(self) -> object: ...


class _OpenpyxlModule(Protocol):
    def load_workbook(self, path: str, read_only: bool = False, data_only: bool = False) -> _Workbook: ...


class _PptxModule(Protocol):
    def Presentation(self, path: str) -> object: ...


def _is_pdfplumber_module(value: object) -> TypeIs[_PdfPlumberModule]:
    return callable(getattr(value, "open", None))


def _is_docx_module(value: object) -> TypeIs[_DocxModule]:
    return callable(getattr(value, "Document", None))


def _is_docx_ns_module(value: object) -> TypeIs[_DocxNsModule]:
    return callable(getattr(value, "qn", None))


def _is_openpyxl_module(value: object) -> TypeIs[_OpenpyxlModule]:
    return callable(getattr(value, "load_workbook", None))


def _is_pptx_module(value: object) -> TypeIs[_PptxModule]:
    return callable(getattr(value, "Presentation", None))


class _Nullary(Protocol):
    def __call__(self, *args: object, **kwargs: object) -> object: ...


def _is_nullary(value: object) -> TypeIs[_Nullary]:
    return callable(value)


def _call_sdk(value: object, *args: object, **kwargs: object) -> object:
    if not _is_nullary(value):
        return None
    return value(*args, **kwargs)


def _object_sequence(value: object) -> list[object]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return list[object](value)
    return []


def _object_iterable(value: object) -> list[object]:
    if isinstance(value, (str, bytes)):
        return []
    if isinstance(value, Sequence):
        return list[object](value)
    if isinstance(value, Iterable):
        return list[object](value)
    return []


def _iter_tagged(container: object, tag: object) -> Iterator[object]:
    iterated = _call_sdk(getattr(container, "iter", None), tag)
    if isinstance(iterated, Sequence) and not isinstance(iterated, (str, bytes)):
        return iter(list[object](iterated))
    if isinstance(iterated, Iterator):
        return iterated
    return iter(())


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
            + "Please split or convert it to a smaller text/Markdown excerpt first."
        )

    ext = file_path.suffix.lower()
    try:
        if ext == ".pdf":
            pdfplumber_mod: object = importlib.import_module("pdfplumber")
            if not _is_pdfplumber_module(pdfplumber_mod):
                return f"Missing dependency: pdfplumber. {_READ_DOCUMENT_INSTALL_HINT}"

            text_parts: list[str] = []
            with pdfplumber_mod.open(str(file_path)) as pdf:
                pdf_obj: object = pdf
                pages = _object_sequence(getattr(pdf_obj, "pages", []))[:50]
                for index, page in enumerate(pages):
                    extracted = _call_sdk(getattr(page, "extract_text", None))
                    page_text = extracted if isinstance(extracted, str) else ""
                    if page_text:
                        text_parts.append(f"--- Page {index + 1} ---\n{page_text}")
                    if sum(len(part) for part in text_parts) >= max_chars:
                        break
            content = "\n\n".join(text_parts) if text_parts else "(PDF is empty or text extraction failed)"
        elif ext == ".docx":
            document_module: object = importlib.import_module("docx")
            document_xml_namespace: object = importlib.import_module("docx.oxml.ns")
            if not _is_docx_module(document_module) or not _is_docx_ns_module(document_xml_namespace):
                return f"Missing dependency: python-docx. {_READ_DOCUMENT_INSTALL_HINT}"

            doc: object = document_module.Document(str(file_path))
            lines: list[str] = []

            def extract_para_text(para: object) -> str:
                text: object = getattr(para, "text", "")
                return text.strip() if isinstance(text, str) else ""

            def extract_table(table: object) -> str:
                rows: list[str] = []
                for row in _object_sequence(getattr(table, "rows", [])):
                    cells = [
                        _safe_document_cell_text(getattr(cell, "text", None)).strip()
                        for cell in _object_sequence(getattr(row, "cells", []))[:_READ_DOCUMENT_MAX_COLUMNS]
                    ]
                    if not cells:
                        continue
                    deduped = [cells[0]] + [cell for index, cell in enumerate(cells[1:]) if cell != cells[index]]
                    row_text = " | ".join(cell for cell in deduped if cell)
                    if row_text:
                        rows.append(row_text)
                return "\n".join(rows)

            for para in _object_sequence(getattr(doc, "paragraphs", [])):
                text = extract_para_text(para)
                if text:
                    lines.append(text)
            for table in _object_sequence(getattr(doc, "tables", [])):
                text = extract_table(table)
                if text:
                    lines.append(text)
            element: object = getattr(doc, "element", None)
            body: object = getattr(element, "body", None)
            textbox_tag: object = document_xml_namespace.qn("w:txbxContent")
            text_tag: object = document_xml_namespace.qn("w:t")
            for shape in _iter_tagged(body, textbox_tag):
                for child in _iter_tagged(shape, text_tag):
                    child_text: object = getattr(child, "text", None)
                    if isinstance(child_text, str) and child_text.strip():
                        lines.append(child_text.strip())
            for section in _object_sequence(object_attr(doc, "sections", [])):
                for header_footer in (object_attr(section, "header", None), object_attr(section, "footer", None)):
                    linked = object_attr(header_footer, "is_linked_to_previous", True)
                    if header_footer is None or linked is not False:
                        continue
                    for para in _object_sequence(object_attr(header_footer, "paragraphs", [])):
                        paragraph_text = extract_para_text(para)
                        if paragraph_text:
                            lines.append(paragraph_text)
            content = "\n".join(lines) if lines else "(Document is empty or uses unsupported formatting)"
        elif ext == ".xlsx":
            openpyxl_mod: object = importlib.import_module("openpyxl")
            if not _is_openpyxl_module(openpyxl_mod):
                return f"Missing dependency: openpyxl. {_READ_DOCUMENT_INSTALL_HINT}"

            workbook = openpyxl_mod.load_workbook(str(file_path), read_only=True, data_only=True)
            sheets: list[str] = []
            cell_count = 0
            sheet_names = [name for name in _object_sequence(workbook.sheetnames) if isinstance(name, str)]
            for ws_name in sheet_names[:10]:
                sheet: object = workbook[ws_name]
                raw_rows = _call_sdk(
                    getattr(sheet, "iter_rows", None),
                    max_row=200,
                    max_col=_READ_DOCUMENT_MAX_COLUMNS,
                    values_only=True,
                )
                rows: list[str] = []
                for raw_row in _object_iterable(raw_rows):
                    row: object = raw_row
                    if not isinstance(row, tuple):
                        continue
                    visible = tuple[object, ...](row)
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
            pptx_mod: object = importlib.import_module("pptx")
            if not _is_pptx_module(pptx_mod):
                return f"Missing dependency: python-pptx. {_READ_DOCUMENT_INSTALL_HINT}"

            presentation: object = pptx_mod.Presentation(str(file_path))
            slides: list[str] = []
            for index, slide in enumerate(_object_sequence(getattr(presentation, "slides", []))[:50]):
                texts: list[str] = []
                for shape in _object_sequence(getattr(slide, "shapes", [])):
                    text_value: object = getattr(shape, "text", None)
                    if isinstance(text_value, str) and text_value.strip():
                        texts.append(text_value)
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
        return f"Missing dependency: {exc}. {_READ_DOCUMENT_INSTALL_HINT}"
    except Exception as exc:
        return f"Document read failed: {str(exc)[:200]}"
