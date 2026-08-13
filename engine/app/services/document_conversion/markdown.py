from __future__ import annotations

import importlib
import re
from pathlib import Path


def _convert_markdown_to_docx(src_file: Path, tgt_file: Path, target_path: str) -> str:
    document_module = importlib.import_module("docx")

    md_text = src_file.read_text(encoding="utf-8")
    doc = document_module.Document()

    def flush_paragraph(lines: list[str]) -> None:
        text = " ".join(line.strip() for line in lines if line.strip()).strip()
        if text:
            doc.add_paragraph(text)

    paragraph_lines: list[str] = []
    lines = md_text.splitlines()
    index = 0
    while index < len(lines):
        line = lines[index].rstrip()
        stripped = line.strip()
        if not stripped:
            flush_paragraph(paragraph_lines)
            paragraph_lines = []
            index += 1
            continue
        heading_match = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if heading_match:
            flush_paragraph(paragraph_lines)
            paragraph_lines = []
            level = min(len(heading_match.group(1)), 6)
            doc.add_heading(heading_match.group(2).strip(), level=level)
            index += 1
            continue
        bullet_match = re.match(r"^[-*+]\s+(.*)$", stripped)
        ordered_match = re.match(r"^\d+\.\s+(.*)$", stripped)
        if bullet_match is not None:
            flush_paragraph(paragraph_lines)
            paragraph_lines = []
            text = bullet_match.group(1).strip()
            if text:
                doc.add_paragraph(text, style="List Bullet")
            index += 1
            continue
        if ordered_match is not None:
            flush_paragraph(paragraph_lines)
            paragraph_lines = []
            text = ordered_match.group(1).strip()
            if text:
                doc.add_paragraph(text, style="List Number")
            index += 1
            continue
        if "|" in stripped:
            table_lines: list[str] = []
            flush_paragraph(paragraph_lines)
            paragraph_lines = []
            while index < len(lines) and "|" in lines[index]:
                candidate = lines[index].strip()
                if candidate:
                    table_lines.append(candidate)
                index += 1
            data_rows = []
            for raw in table_lines:
                cells = [cell.strip() for cell in raw.strip("|").split("|")]
                if cells and all(re.fullmatch(r":?-{3,}:?", cell.replace(" ", "")) for cell in cells):
                    continue
                if any(cell for cell in cells):
                    data_rows.append(cells)
            if data_rows:
                table = doc.add_table(rows=len(data_rows), cols=max(len(row) for row in data_rows))
                table.style = "Table Grid"
                for row_index, row in enumerate(data_rows):
                    for column_index, cell in enumerate(row):
                        table.cell(row_index, column_index).text = cell
            continue
        paragraph_lines.append(stripped)
        index += 1

    flush_paragraph(paragraph_lines)
    tgt_file.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(tgt_file))
    return f"✅ Successfully converted Markdown to Word: {target_path}"


def _convert_markdown_to_pdf(src_file: Path, tgt_file: Path, target_path: str, ws: Path) -> str:
    weasyprint = importlib.import_module("weasyprint")

    md_text = src_file.read_text(encoding="utf-8")

    def escape_html(text: str) -> str:
        return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")

    def render_inline(text: str) -> str:
        rendered = escape_html(text)
        rendered = re.sub(r"\*\*\*(.*?)\*\*\*", r"<strong><em>\1</em></strong>", rendered)
        rendered = re.sub(r"\*\*(.*?)\*\*", r"<strong>\1</strong>", rendered)
        rendered = re.sub(r"__(.*?)__", r"<strong>\1</strong>", rendered)
        rendered = re.sub(r"\*(.*?)\*", r"<em>\1</em>", rendered)
        rendered = re.sub(r"_(.*?)_", r"<em>\1</em>", rendered)
        rendered = re.sub(r"`([^`]+)`", r"<code>\1</code>", rendered)
        return re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', rendered)

    def is_table_separator(line: str) -> bool:
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        return bool(cells) and all(re.fullmatch(r":?-{3,}:?", cell or "") for cell in cells)

    html_parts: list[str] = []
    lines = md_text.splitlines()
    in_list = False
    index = 0
    while index < len(lines):
        stripped = lines[index].rstrip().strip()
        if not stripped:
            if in_list:
                html_parts.append("</ul>")
                in_list = False
            index += 1
            continue
        heading_match = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if heading_match:
            if in_list:
                html_parts.append("</ul>")
                in_list = False
            level = len(heading_match.group(1))
            html_parts.append(f"<h{level}>{render_inline(heading_match.group(2).strip())}</h{level}>")
            index += 1
            continue
        bullet_match = re.match(r"^[-*+]\s+(.*)$", stripped)
        if bullet_match:
            if not in_list:
                html_parts.append("<ul>")
                in_list = True
            html_parts.append(f"<li>{render_inline(bullet_match.group(1).strip())}</li>")
            index += 1
            continue
        if "|" in stripped and index + 1 < len(lines) and is_table_separator(lines[index + 1].strip()):
            if in_list:
                html_parts.append("</ul>")
                in_list = False
            headers = [render_inline(cell.strip()) for cell in stripped.strip("|").split("|")]
            table_rows: list[list[str]] = []
            index += 2
            while index < len(lines) and "|" in lines[index].strip():
                table_rows.append([render_inline(cell.strip()) for cell in lines[index].strip().strip("|").split("|")])
                index += 1
            html_parts.append(
                "<table><thead><tr>" + "".join(f"<th>{cell}</th>" for cell in headers) + "</tr></thead><tbody>"
            )
            html_parts.extend("<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>" for row in table_rows)
            html_parts.append("</tbody></table>")
            continue
        if in_list:
            html_parts.append("</ul>")
            in_list = False
        html_parts.append(f"<p>{render_inline(stripped)}</p>")
        index += 1
    if in_list:
        html_parts.append("</ul>")

    html_text = "\n".join(html_parts)
    full_html = (
        "<html><head><meta charset='utf-8'><style>"
        "body{font-family:'WenQuanYi Micro Hei','Noto Sans CJK SC',sans-serif;line-height:1.65;padding:2em;color:#111827;}"
        "h1,h2,h3{line-height:1.25;margin:1.2em 0 .55em;}"
        "p{margin:.55em 0;}"
        "table{width:100%;border-collapse:collapse;margin:1em 0;font-size:12px;}"
        "th,td{border:1px solid #d8dee9;padding:7px 9px;text-align:left;vertical-align:top;}"
        "th{background:#f3f4f6;font-weight:700;}"
        "code{background:#f3f4f6;padding:1px 4px;border-radius:4px;}"
        "a{color:#2563eb;text-decoration:none;}"
        "</style></head><body>"
        f"{html_text}"
        "</body></html>"
    )
    tgt_file.parent.mkdir(parents=True, exist_ok=True)
    weasyprint.HTML(string=full_html, base_url=str(ws.resolve())).write_pdf(str(tgt_file))
    return f"✅ Successfully converted Markdown to PDF: {target_path}"
