"""Content-Disposition header values for file downloads, shared by presigned URLs and the responses that serve files."""

from typing import Literal
from urllib.parse import quote


def content_disposition(filename: str, disposition: Literal["inline", "attachment"] = "inline") -> str:
    """A header value whose plain and RFC 5987 names cannot break out of the header."""
    ascii_name = "".join(char if char.isascii() and (char.isalnum() or char in "._- ") else "_" for char in filename)
    return f"{disposition}; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename, safe='')}"
