"""Incremental UTF-8 windows and Python splitlines-compatible logical lines."""

import codecs
import re
from dataclasses import dataclass

from .workspace_search_cursor import FilePosition
from .workspace_search_spill import append_fragment, read_fragments

_BREAK = re.compile(r"\r\n|[\n\r\v\f\x1c-\x1e\x85\u2028\u2029]")


@dataclass(frozen=True, slots=True)
class SearchLine:
    text: str
    number: int
    rest: FilePosition | None


async def take_line(pending: FilePosition, started: int) -> SearchLine | FilePosition | None:
    boundary = _BREAK.search(pending.text)
    if boundary is not None and not (
        boundary.group() == "\r" and boundary.end() == len(pending.text) and not pending.eof
    ):
        text = pending.text[: boundary.start()]
        rest = pending.model_copy(
            update={"text": pending.text[boundary.end() :], "line": pending.line + 1, "fragments": ""}
        )
    elif pending.eof:
        if not pending.text and not pending.fragments:
            return None
        text, rest = pending.text, None
    else:
        held = "\r" if pending.text.endswith("\r") else ""
        fragment = pending.text[: -len(held)] if held else pending.text
        tail = await append_fragment(pending.fragments, fragment, started) if fragment else pending.fragments
        return pending.model_copy(update={"text": held, "fragments": tail})
    if pending.fragments:
        text = await read_fragments(pending.fragments) + text
    if rest is not None and rest.eof and not rest.text:
        rest = None
    return SearchLine(text, pending.line, rest)


def feed_window(pending: FilePosition, data: bytes, requested: int) -> FilePosition:
    decoder = codecs.getincrementaldecoder("utf-8")(errors="ignore")
    decoder.setstate((bytes.fromhex(pending.utf8), 0))
    eof = len(data) < requested
    text = decoder.decode(data, final=eof)
    buffered, _flag = decoder.getstate()
    return pending.model_copy(
        update={"text": pending.text + text, "offset": pending.offset + len(data), "utf8": buffered.hex(), "eof": eof}
    )
