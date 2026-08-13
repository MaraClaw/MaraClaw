from __future__ import annotations

import importlib
from typing import TypedDict


class _TextRun(TypedDict):
    content: str


class _BlockElement(TypedDict):
    text_run: _TextRun


class _BlockContent(TypedDict):
    elements: list[_BlockElement]


def _markdown_module():
    return importlib.import_module("app.services.agent_tool_exec._agent_tool_exec_feishu_markdown")


def _block_text(block: dict[str, _BlockContent], key: str) -> str:
    return "".join(element["text_run"]["content"] for element in block[key]["elements"])


def test_parse_inline_markdown_preserves_styles_and_backticks_are_plain_text() -> None:
    target = _markdown_module()

    elements = target._parse_inline_markdown("A **bold** *italic* ~~gone~~ `code` tail")

    assert elements == [
        {"text_run": {"content": "A "}},
        {"text_run": {"content": "bold", "text_element_style": {"bold": True}}},
        {"text_run": {"content": " "}},
        {"text_run": {"content": "italic", "text_element_style": {"italic": True}}},
        {"text_run": {"content": " "}},
        {"text_run": {"content": "gone", "text_element_style": {"strikethrough": True}}},
        {"text_run": {"content": " "}},
        {"text_run": {"content": "code"}},
        {"text_run": {"content": " tail"}},
    ]


def test_markdown_to_feishu_blocks_characterizes_supported_block_shapes() -> None:
    target = _markdown_module()
    markdown = "\n".join(
        [
            "# Heading 1",
            "#### Heading 4",
            "- Bullet **bold**",
            "1. Ordered *item*",
            "> Quote ~~gone~~",
            "```python",
            "print('hello')",
            "```",
            "---",
            "| Name | Value |",
            "| --- | --- |",
            "| Alice | 42 |",
            "plain `code`",
        ]
    )

    blocks = target._markdown_to_feishu_blocks(markdown)

    assert [block["block_type"] for block in blocks] == [3, 6, 12, 13, 15, 14, 2, 2, 2, 2]
    assert _block_text(blocks[0], "heading1") == "Heading 1"
    assert _block_text(blocks[1], "heading4") == "Heading 4"
    assert blocks[2]["bullet"]["elements"][1] == {"text_run": {"content": "bold", "text_element_style": {"bold": True}}}
    assert blocks[3]["ordered"]["elements"][1] == {
        "text_run": {"content": "item", "text_element_style": {"italic": True}}
    }
    assert blocks[4]["quote"]["elements"][1] == {
        "text_run": {"content": "gone", "text_element_style": {"strikethrough": True}}
    }
    assert blocks[5] == {
        "block_type": 14,
        "code": {
            "elements": [{"text_run": {"content": "print('hello')"}}],
            "style": {"language": 49},
        },
    }
    assert blocks[6] == {"block_type": 2, "text": {"elements": [{"text_run": {"content": "\u2500" * 24}}]}}
    assert _block_text(blocks[7], "text") == "Name  |  Value"
    assert _block_text(blocks[8], "text") == "Alice  |  42"
    assert blocks[9]["text"]["elements"] == [{"text_run": {"content": "plain "}}, {"text_run": {"content": "code"}}]
