"""The start of a stored text file, read in one ranged read instead of the whole object.

Prompt builders and the page publisher only need the first part of a workspace file, and a guest or a member can make that
file any size. `read_text_head` takes at most `max_bytes` from the start and `read_text_capped` turns that into prompt
text of at most a number of characters, so what the engine holds never depends on the file.
"""

import codecs
from dataclasses import dataclass
from typing import Final

from app.services.storage_runtime.base import StorageBackend

TRUNCATED_MARKER: Final = "\n...(truncated)"


@dataclass(frozen=True, slots=True)
class TextHead:
    """The decoded start of a file; `more` is true when the file may continue past it (the read filled its budget)."""

    text: str
    more: bool


def head_bytes_for_chars(max_chars: int) -> int:
    """Bytes that always hold `max_chars` complete characters: UTF-8 takes at most 4 per character, and a character cut
    at the end of the head can take up to 3 more bytes before it is dropped."""
    return max_chars * 4 + 4


async def read_text_head(storage: StorageBackend, key: str, max_bytes: int) -> TextHead:
    """Decode the first `max_bytes` of `key` as UTF-8, replacing invalid bytes as a whole-file read does.

    A file shorter than `max_bytes` decodes exactly as `StorageBackend.read_text` would, an incomplete character at its
    very end included. When the read filled the budget the file may go on, so a character cut by the end of the head is
    dropped rather than turned into U+FFFD. Raises `FileNotFoundError` for an absent key.
    """
    data = await storage.read_bytes_bounded(key, max_bytes, offset=0)
    more = len(data) >= max_bytes
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    return TextHead(decoder.decode(data, final=not more), more)


async def read_text_capped(storage: StorageBackend, key: str, max_chars: int) -> str:
    """The file's text stripped and cut to `max_chars`, ending in `TRUNCATED_MARKER` when the file had more.

    A head that is all whitespace gives `""` however long the file is, so callers treat it as empty, not as a marker.
    """
    head = await read_text_head(storage, key, head_bytes_for_chars(max_chars))
    text = head.text.strip()
    if text and (head.more or len(text) > max_chars):
        return text[:max_chars] + TRUNCATED_MARKER
    return text
