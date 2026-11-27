"""A window of numbered lines taken from text fed in pieces, with ``str.splitlines`` line breaks.

``read_file`` streams a file through ``LineWindow`` instead of splitting it whole: lines before the window are only
counted, the lines inside it are kept as output rows, and the line count goes on after it only until the caller stops.
What is held never depends on the file: the rows (at most ``MAX_WINDOW_CHARS`` characters) and one line in progress
(at most ``MAX_LINE_CHARS`` of it kept).
"""

from typing import Final

MAX_LINE_CHARS: Final = 16 * 1024
MAX_WINDOW_CHARS: Final = 1024 * 1024
# The characters ``str.splitlines`` breaks on; "\r\n" is one break made of two of them.
_LINE_BREAKS: Final = "\n\r\x0b\x0c\x1c\x1d\x1e\x85\u2028\u2029"


class LineWindow:
    """Lines ``start`` to ``start + limit - 1`` (0-based) of the text fed so far, as rows like ``"     3\\ttext"``.

    A line longer than ``MAX_LINE_CHARS`` keeps its first characters and ends with a marker naming how many were left
    out. The window also closes before a line that would take ``rows`` past ``MAX_WINDOW_CHARS`` (the first row is
    always kept, so a call that finds a line makes progress). ``more`` turns true as soon as a line exists after the
    window; ``finish`` returns the number of lines once the whole text was fed.
    """

    def __init__(self, start: int, limit: int) -> None:
        self.start: int = start
        self.limit: int = limit
        self.rows: list[str] = []
        self.more: bool = False
        self._index: int = 0
        self._kept: str = ""
        self._length: int = 0
        self._room: int = MAX_WINDOW_CHARS
        self._skip_lf: bool = False

    def feed(self, text: str) -> None:
        if not text:
            return
        if self._skip_lf:
            text = text.removeprefix("\n")
        # A "\r" ending the piece may be the first half of "\r\n": the "\n" that starts the next piece is not a line.
        self._skip_lf = text.endswith("\r")
        if not text:
            return
        parts = text.splitlines(keepends=True)
        ended = len(parts) if text[-1] in _LINE_BREAKS else len(parts) - 1
        # Lines outside the window are only counted, so whole runs of them are skipped without a per-line step.
        taken = min(ended, max(0, self.start - self._index))
        self._skip(taken)
        while taken < ended and not self.more:
            part = parts[taken]
            self._extend(part[: -2 if part.endswith("\r\n") else -1])
            self._close_line()
            taken += 1
        self._skip(ended - taken)
        if ended < len(parts):
            self._extend(parts[-1])

    def finish(self) -> int:
        if self._length:
            self._close_line()
        return self._index

    def _skip(self, lines: int) -> None:
        if lines:
            self._index += lines
            self._kept = ""
            self._length = 0

    def _extend(self, piece: str) -> None:
        if not piece:
            return
        self._length += len(piece)
        if self._index < self.start or self.more:
            return
        if len(self.rows) >= self.limit:
            self.more = True
        else:
            self._kept += piece[: MAX_LINE_CHARS - len(self._kept)]

    def _close_line(self) -> None:
        if self._index >= self.start and not self.more:
            text = self._kept
            if self._length > len(text):
                text += f"... [line truncated: {self._length - len(text)} more characters]"
            row = f"{self._index + 1:6}\t{text}"
            if len(self.rows) >= self.limit or (self.rows and len(row) + 1 > self._room):
                self.more = True
            else:
                self.rows.append(row)
                self._room -= len(row) + 1
        self._index += 1
        self._kept = ""
        self._length = 0
