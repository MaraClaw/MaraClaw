"""Bounded, authenticated search positions over live file byte offsets.

Captured windows/fragments survive changes; subsequent ranges use current bytes at
the saved offset and can mix versions. Deletion is unreadable; truncation ends the
file at the next range. Inserts behind directory frontiers are omitted and renames
can repeat. There is no snapshot isolation. Searches expire one hour after start.
"""

from base64 import urlsafe_b64encode
from hashlib import sha256
from time import time
from typing import Annotated, ClassVar, Final, Literal, TypedDict, assert_never

from cryptography.fernet import Fernet, InvalidToken
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from app.config import get_settings

FILE_BYTES: Final = 256 * 1024
CONTENT_BYTES: Final = 1024 * 1024
SCAN_STEPS: Final = 128
LINE_STEPS: Final = 10_000
MAX_CURSOR_LENGTH: Final = 3 * 1024 * 1024
CURSOR_TTL: Final = 3600


class SearchContinuation(TypedDict, total=False):
    cursor: str
    limit: int


class SearchQuery(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid", strict=True)

    agent: str
    tenant: str | None
    root: str
    display: str
    pattern: str
    file_pattern: str
    ignore_case: bool
    limit: int = Field(ge=1, le=50)


class DirectoryPosition(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid", strict=True)

    key: str
    after: str = ""
    done: bool = False


class FilePosition(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid", strict=True)

    key: str
    text: str = Field(default="", max_length=FILE_BYTES + 1)
    line: int = Field(default=1, ge=1)
    offset: int = Field(default=0, ge=0)
    utf8: str = Field(default="", pattern=r"^(?:[0-9a-f]{2}){0,3}$")
    eof: bool = False
    fragments: str = Field(default="", pattern=r"^(?:[0-9a-f]{32})?$")


class _StoredPosition(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid", strict=True)

    version: Literal[5] = 5
    binding: str = Field(pattern=r"^[0-9a-f]{64}$")
    token: str = Field(pattern=r"^[0-9a-f]{32}$")
    started: int = Field(ge=0)


class SearchPosition(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid", strict=True)

    version: Literal[4] = 4
    query: SearchQuery
    directories: tuple[DirectoryPosition, ...]
    pending: FilePosition | None = None
    unreadable: int = Field(default=0, ge=0)
    started: int = Field(default_factory=lambda: int(time()), ge=0)

    async def encode(self) -> str:
        prefix = self.query.root
        directories: list[DirectoryPosition] = []
        for directory in self.directories:
            relative = "" if directory.key == prefix else directory.key.removeprefix(prefix + "/")
            directories.append(directory.model_copy(update={"key": relative}))
            prefix = directory.key
        compact = self.model_copy(update={"directories": tuple(directories)})
        payload = compact.model_dump_json()
        cursor = _cipher().encrypt(payload.encode()).decode()
        if len(cursor) > MAX_CURSOR_LENGTH:
            from .workspace_search_spill import append_fragment

            token = await append_fragment("", payload, self.started)
            stored = _StoredPosition(
                binding=sha256(self.query.model_dump_json().encode()).hexdigest(), token=token, started=self.started
            )
            cursor = _cipher().encrypt(stored.model_dump_json().encode()).decode()
        return cursor


_POSITION = TypeAdapter(Annotated[SearchPosition | _StoredPosition, Field(discriminator="version")])


class InvalidSearchCursorError(ValueError):
    def __init__(self) -> None:
        super().__init__(
            "Invalid search_files cursor: malformed, altered, expired, unavailable, or bound to another search"
        )


def _cipher() -> Fernet:
    key = sha256(("workspace-search-v1:" + get_settings().SECRET_KEY).encode()).digest()
    return Fernet(urlsafe_b64encode(key))


async def parse_position(raw: str | None, query: SearchQuery) -> SearchPosition:
    if raw is None:
        return SearchPosition(query=query, directories=(DirectoryPosition(key=query.root),))
    if not raw or len(raw) > MAX_CURSOR_LENGTH:
        raise InvalidSearchCursorError
    try:
        decoded = _POSITION.validate_json(_cipher().decrypt(raw.encode()), strict=True)
        match decoded:
            case SearchPosition():
                position = decoded
            case _StoredPosition():
                from .workspace_search_spill import read_fragments

                binding = sha256(query.model_dump_json().encode()).hexdigest()
                if decoded.binding != binding or not 0 <= time() - decoded.started <= CURSOR_TTL:
                    raise InvalidSearchCursorError
                position = SearchPosition.model_validate_json(await read_fragments(decoded.token), strict=True)
            case unreachable:
                assert_never(unreachable)
    except InvalidToken, ValueError:
        raise InvalidSearchCursorError from None
    if position.query != query or not 0 <= time() - position.started <= CURSOR_TTL:
        raise InvalidSearchCursorError
    prefix = query.root
    directories: list[DirectoryPosition] = []
    for directory in position.directories:
        prefix = f"{prefix}/{directory.key}" if directory.key else prefix
        directories.append(directory.model_copy(update={"key": prefix}))
    return position.model_copy(update={"directories": tuple(directories)})
