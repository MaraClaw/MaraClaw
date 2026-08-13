from __future__ import annotations

import re
from typing import Final

from app.services.agent_tool_exec.registry import ToolArgumentValue

_HEADING_BLOCKS: Final[dict[int, tuple[int, str]]] = {
    1: (3, "heading1"),
    2: (4, "heading2"),
    3: (5, "heading3"),
    4: (6, "heading4"),
}


def _make_run(content: str, style: dict[str, ToolArgumentValue] | None = None) -> dict[str, ToolArgumentValue]:
    run: dict[str, ToolArgumentValue] = {"content": content}
    if style:
        run["text_element_style"] = style
    return {"text_run": run}


def _parse_inline_markdown(text: str) -> list[ToolArgumentValue]:
    elements: list[ToolArgumentValue] = []
    pattern = r"(\*\*(.+?)\*\*|\*(.+?)\*|~~(.+?)~~|`(.+?)`)"
    position = 0
    for match in re.finditer(pattern, text):
        if match.start() > position:
            elements.append(_make_run(text[position : match.start()]))
        raw = match.group(0)
        if raw.startswith("**"):
            elements.append(_make_run(match.group(2), {"bold": True}))
        elif raw.startswith("~~"):
            elements.append(_make_run(match.group(4), {"strikethrough": True}))
        elif raw.startswith("`"):
            elements.append(_make_run(match.group(5)))
        else:
            elements.append(_make_run(match.group(3), {"italic": True}))
        position = match.end()
    if position < len(text):
        elements.append(_make_run(text[position:]))
    if not elements:
        elements.append(_make_run(text or " "))
    return elements


def _text_block(block_type: int, key: str, line: str) -> dict[str, ToolArgumentValue]:
    text: dict[str, ToolArgumentValue] = {"elements": _parse_inline_markdown(line)}
    return {
        "block_type": block_type,
        key: text,
    }


def _markdown_to_feishu_blocks(markdown: str) -> list[dict[str, ToolArgumentValue]]:
    blocks: list[dict[str, ToolArgumentValue]] = []
    lines = markdown.splitlines()
    line_index = 0
    while line_index < len(lines):
        line = lines[line_index]
        stripped = line.strip()

        if stripped.startswith("```"):
            language = stripped[3:].strip()
            code_lines = []
            line_index += 1
            while line_index < len(lines) and not lines[line_index].strip().startswith("```"):
                code_lines.append(lines[line_index])
                line_index += 1
            code_elements: list[ToolArgumentValue] = [{"text_run": {"content": "\n".join(code_lines)}}]
            code: dict[str, ToolArgumentValue] = {
                "elements": code_elements,
                "style": {"language": _code_language(language)},
            }
            blocks.append({"block_type": 14, "code": code})
            line_index += 1
            continue

        if re.fullmatch(r"[-*_]{3,}", stripped):
            blocks.append({"block_type": 2, "text": {"elements": [_make_run("\u2500" * 24)]}})
            line_index += 1
            continue

        heading_match = re.match(r"^(#{1,4})\s+(.*)", line)
        if heading_match:
            level = min(len(heading_match.group(1)), 4)
            block_type, key = _HEADING_BLOCKS[level]
            blocks.append(_text_block(block_type, key, heading_match.group(2)))
            line_index += 1
            continue

        if re.match(r"^[\-*+]\s+", line):
            blocks.append(_text_block(12, "bullet", re.sub(r"^[\-*+]\s+", "", line)))
            line_index += 1
            continue

        if re.match(r"^\d+\.\s+", line):
            blocks.append(_text_block(13, "ordered", re.sub(r"^\d+\.\s+", "", line)))
            line_index += 1
            continue

        if line.startswith("> "):
            blocks.append(_text_block(15, "quote", line[2:]))
            line_index += 1
            continue

        if stripped == "":
            blocks.append({"block_type": 2, "text": {"elements": [_make_run(" ")]}})
            line_index += 1
            continue

        if re.match(r"^\|[\s\-:]+(\|[\s\-:]+)*\|?\s*$", stripped):
            line_index += 1
            continue

        if stripped.startswith("|") and stripped.endswith("|"):
            cells = [cell.strip() for cell in stripped.strip("|").split("|")]
            blocks.append(_text_block(2, "text", "  |  ".join(cell for cell in cells if cell)))
            line_index += 1
            continue

        blocks.append(_text_block(2, "text", line))
        line_index += 1

    return blocks


def _code_language(language: str) -> int:
    return {
        "python": 49,
        "javascript": 22,
        "js": 22,
        "typescript": 56,
        "ts": 56,
        "bash": 4,
        "sh": 4,
        "sql": 53,
        "java": 21,
        "go": 17,
        "rust": 51,
        "json": 25,
        "yaml": 60,
        "html": 19,
        "css": 10,
    }.get(language.lower(), 1)
